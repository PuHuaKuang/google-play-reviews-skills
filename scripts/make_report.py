"""把 report.json 渲染成单页 HTML 看板（深灰 + 翠绿的 BI 终端风格）。

用法：
  python make_report.py --report report.json --out review_report.html
"""

from __future__ import annotations

import argparse
import json

CSS = """
:root{--bg:#1a1d21;--panel:#22262b;--panel2:#2a2f35;--line:#343a41;
--txt:#e6e9ec;--dim:#8b949e;--accent:#00d47e;--warn:#ffb020;--bad:#ff5c5c;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"Segoe UI",
"PingFang SC","Microsoft YaHei",sans-serif;padding:28px}
h1{font-size:22px;font-weight:600;letter-spacing:.5px}
h2{font-size:15px;font-weight:600;color:var(--accent);margin:0 0 14px;
letter-spacing:.5px;display:flex;align-items:center;gap:8px}
h2::before{content:"";width:3px;height:14px;background:var(--accent);border-radius:2px}
.sub{color:var(--dim);font-size:12px;margin-top:6px}
.grid{display:grid;gap:16px;margin-top:20px}
.g4{grid-template-columns:repeat(4,1fr)}
.g2{grid-template-columns:1fr 1fr}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}
.kpi{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.kpi-l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:1px}
.kpi-n{font-size:11px;color:var(--dim);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;font-size:11px;
text-transform:uppercase;letter-spacing:.6px;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #2b3037;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.num{text-align:right}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;min-width:60px}
.bar>i{display:block;height:100%;background:var(--accent);border-radius:3px}
.bar>i.w{background:var(--warn)}.bar>i.b{background:var(--bad)}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:500}
.up{background:rgba(255,92,92,.15);color:var(--bad)}
.down{background:rgba(0,212,126,.15);color:var(--accent)}
.flat{background:var(--panel2);color:var(--dim)}
.spark{display:flex;align-items:flex-end;gap:2px;height:28px}
.spark>i{flex:1;background:var(--accent);opacity:.65;border-radius:1px 1px 0 0;min-height:2px}
.spark>i:last-child{opacity:1}
.q{background:var(--panel2);border-left:2px solid var(--line);padding:8px 11px;
border-radius:0 6px 6px 0;margin-bottom:7px;font-size:12px;color:#c9d1d9}
.q b{color:var(--bad);font-weight:600}
.q em{color:var(--dim);font-style:normal;font-size:11px}
.priority{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}.p0{background:rgba(255,92,92,.2);color:var(--bad)}.p1{background:rgba(255,176,32,.18);color:var(--warn)}.p2{background:rgba(0,212,126,.16);color:var(--accent)}
td em{color:var(--dim);font-style:normal;font-size:11px}.action-table{font-size:12px}.action-table td{vertical-align:top;line-height:1.5}.action-table td:nth-child(4){min-width:330px}.action-table td:nth-child(5){color:var(--accent);min-width:150px}
.note{background:rgba(255,176,32,.08);border:1px solid rgba(255,176,32,.25);
border-radius:8px;padding:12px 14px;font-size:12px;color:#d9c9a8;margin-top:16px}
footer{margin-top:24px;color:var(--dim);font-size:11px;text-align:center}
"""


def pct(v, digits=1):
    return f"{v * 100:.{digits}f}%"


def bar(ratio, cls=""):
    w = min(100, max(2, ratio * 100))
    return f'<div class="bar"><i class="{cls}" style="width:{w:.1f}%"></i></div>'


def trend_tag(v):
    if v is None:
        return '<span class="tag flat">—</span>'
    if v > 0.08:
        return f'<span class="tag up">▲ {v:+.0%}</span>'
    if v < -0.08:
        return f'<span class="tag down">▼ {v:+.0%}</span>'
    return f'<span class="tag flat">{v:+.0%}</span>'


def spark(values):
    if not values:
        return ""
    mx = max(values) or 1
    bars = "".join(f'<i style="height:{v/mx*100:.0f}%"></i>' for v in values)
    return f'<div class="spark">{bars}</div>'


def build(r: dict) -> str:
    o = r["overall"]
    months = r["monthly_trend"]
    meta = r.get("meta", {})
    complete = set(meta.get("complete_months", []))
    package = meta.get("package") or "未指定包名"
    # 时间范围只统计具备分析价值的月份，避免被少量历史残留记录拉长成数年
    core = [m for m in months if not m.get("low_sample")] or months
    span = f"{core[0]['month']} ~ {core[-1]['month']}" if core else "时间范围未知"
    month_count = len(core)
    partial_months = [m["month"] for m in months if m.get("truncated")]
    low_sample_months = [m["month"] for m in months if m.get("low_sample")]
    notes = []
    if partial_months:
        notes.append(f"数据截断月（{'、'.join(partial_months)}）已从计数类环比中排除")
    if low_sample_months:
        notes.append(f"低样本历史月（{'、'.join(low_sample_months[:6])}{'等' if len(low_sample_months) > 6 else ''}）未纳入趋势基线")
    partial_note = "；".join(notes) + "。" if notes else "本次数据未检测到需要排除的月份。"

    kpis = f"""
    <div class="grid g4">
      <div class="card"><div class="kpi-l">评论总量</div>
        <div class="kpi">{o['raw_total']:,}</div>
        <div class="kpi-n">{month_count} 个月，含纯星级评分</div></div>
      <div class="card"><div class="kpi-l">平均星级</div>
        <div class="kpi" style="color:var(--accent)">{o['raw_avg_rating']}</div>
        <div class="kpi-n">满分 5.0</div></div>
      <div class="card"><div class="kpi-l">差评率</div>
        <div class="kpi" style="color:var(--warn)">{pct(o['raw_negative_rate'],2)}</div>
        <div class="kpi-n">{o['raw_negative']:,} 条 1-2 星</div></div>
      <div class="card"><div class="kpi-l">有效语料</div>
        <div class="kpi">{o['text_total']:,}</div>
        <div class="kpi-n">占比 {pct(o['text_total']/o['raw_total'])}，含技术线索 {o['text_with_tech_hint']}</div></div>
    </div>"""

    mrows = ""
    for m in months:
        flag = "" if m["month"] in complete else ' <em style="color:var(--warn)">(残缺)</em>'
        cls = "b" if m["negative_rate"] > 0.055 else ("w" if m["negative_rate"] > 0.05 else "")
        mrows += f"""<tr><td>{m['month']}{flag}</td>
          <td class="num">{m['count']:,}</td>
          <td class="num">{m['avg_rating']}</td>
          <td class="num">{pct(m['negative_rate'],2)}</td>
          <td style="width:120px">{bar(m['negative_rate']/0.07, cls)}</td></tr>"""

    trows = ""
    for t in r["topics"]:
        vals = [v for _, v in sorted(t["by_month"].items())]
        cls = "b" if t["negative_ratio"] > 0.4 else ("w" if t["negative_ratio"] > 0.25 else "")
        trows += f"""<tr><td>{t['topic']}</td>
          <td class="num">{t['mentions']}</td>
          <td class="num" style="color:var(--bad)">{t['negative_mentions']}</td>
          <td class="num">{pct(t['negative_ratio'])}</td>
          <td style="width:90px">{bar(t['negative_ratio'], cls)}</td>
          <td>{trend_tag(t['mom_vs_baseline'])}</td>
          <td style="width:80px">{spark(vals)}</td></tr>"""

    drows = ""
    for d in r["device_risk"][:10]:
        cls = "b" if d["negative_rate"] > 0.2 else "w"
        drows += f"""<tr><td>{d['device']}</td>
          <td class="num">{d['count']}</td>
          <td class="num">{d['avg_rating']}</td>
          <td class="num">{pct(d['negative_rate'])}</td>
          <td style="width:90px">{bar(d['negative_rate']/0.3, cls)}</td></tr>"""

    vrows = ""
    for v in r["version_risk"][:10]:
        cls = "b" if v["negative_rate"] > 0.08 else "w"
        vrows += f"""<tr><td style="font-size:11px">{v['version'][:26]}</td>
          <td class="num">{v['count']}</td>
          <td class="num">{v['avg_rating']}</td>
          <td class="num">{pct(v['negative_rate'])}</td>
          <td style="width:80px">{bar(v['negative_rate']/0.15, cls)}</td></tr>"""

    lrows = ""
    for l in r["lang_dist"][:10]:
        cls = "b" if l["negative_rate"] > 0.18 else ("w" if l["negative_rate"] > 0.13 else "")
        lrows += f"""<tr><td>{l['lang']}</td>
          <td class="num">{l['count']}</td>
          <td class="num">{l['avg_rating']}</td>
          <td class="num">{pct(l['negative_rate'])}</td>
          <td style="width:90px">{bar(l['negative_rate']/0.25, cls)}</td></tr>"""

    quotes = ""
    for t in r["topics"][:5]:
        if not t["samples"]:
            continue
        quotes += f'<div style="margin-bottom:14px"><div style="color:var(--accent);font-size:12px;margin-bottom:6px">{t["topic"]}</div>'
        for s in t["samples"][:2]:
            txt = (s["text"] or "").replace("<", "&lt;")[:170]
            quotes += (f'<div class="q"><b>{s["star"]}★</b> '
                       f'<em>{s["lang"]} / {s["device"] or "-"}</em><br>{txt}</div>')
        quotes += "</div>"

    dups = ""
    for d in r["spam"]["duplicate_clusters"][:8]:
        s = (d["sample"] or "").replace("<", "&lt;")[:40]
        dups += (f'<tr><td>{s}</td><td class="num">{d["count"]}</td>'
                 f'<td class="num">{d["avg_rating"]}</td></tr>')

    crows = ""
    for c in r.get("country_dist", [])[:12]:
        cls = "b" if c["negative_rate"] > 0.2 else ("w" if c["negative_rate"] > 0.13 else "")
        langs = ", ".join(c.get("languages", []))
        crows += f"""<tr><td>{c['country']}</td>
          <td class="num">{c['count']:,}</td><td class="num">{c['avg_rating']}</td>
          <td class="num">{pct(c['negative_rate'])}</td>
          <td style="width:90px">{bar(c['negative_rate']/0.25, cls)}</td>
          <td style="font-size:11px;color:var(--dim)">{langs}</td></tr>"""

    arows = ""
    for a in r.get("action_items", []):
        pcls = "p0" if a["priority"] == "P0" else ("p1" if a["priority"] == "P1" else "p2")
        arows += f"""<tr><td><span class="priority {pcls}">{a['priority']}</span></td>
          <td><b>{a['topic']}</b><br><em>{a['evidence']}</em></td>
          <td>{a['owner']}</td><td>{a['action']}</td><td>{a['verify']}</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>应用评论洞察看板 · {package}</title>
<style>{CSS}</style></head><body>
<h1>应用评论洞察看板</h1>
<div class="sub">{package} · {span} · 数据源：{meta.get('source_label') or '应用商店导出或评论 API'}</div>

{kpis}

<div class="grid">
  <div class="card"><h2>行动清单：从发现到验证</h2>
    <table class="action-table"><thead><tr><th>优先级</th><th>问题证据</th><th>建议负责人</th><th>下一步行动</th><th>验证指标</th></tr></thead>
    <tbody>{arows}</tbody></table>
    <div class="sub" style="margin-top:10px">排序原则：差评影响规模 × 差评占比 × 上升趋势；P0 先处理影响面最大的问题，P1 处理明显的区域或专项风险，P2 进入持续优化。</div></div>
</div>

<div class="grid g2">
  <div class="card"><h2>月度口碑走势</h2>
    <table><thead><tr><th>月份</th><th class="num">评论量</th><th class="num">均分</th>
      <th class="num">差评率</th><th></th></tr></thead><tbody>{mrows}</tbody></table></div>
  <div class="card"><h2>国家/市场差评分布（按语言推断）</h2>
    <table><thead><tr><th>国家/市场</th><th class="num">语料</th><th class="num">均分</th>
      <th class="num">差评率</th><th></th><th>语言代码</th></tr></thead><tbody>{crows}</tbody></table>
    <div class="sub" style="margin-top:10px">注意：仅在源数据没有可信国家字段时，才根据 Reviewer Language 生成市场代理；英语、西语、阿语等多国共用语言不拆分为单一国家。</div></div>
</div>

<div class="grid">
  <div class="card"><h2>问题主题排行（按差评提及数）</h2>
    <table><thead><tr><th>主题</th><th class="num">提及</th><th class="num">差评</th>
      <th class="num">差评占比</th><th></th><th>环比</th><th>月度分布</th></tr></thead>
      <tbody>{trows}</tbody></table>
    <div class="sub" style="margin-top:10px">环比 = 最后一个完整月 vs 前序月份均值。
      {partial_note}</div></div>
</div>

<div class="grid g2">
  <div class="card"><h2>高风险机型 TOP10</h2>
    <table><thead><tr><th>机型</th><th class="num">样本</th><th class="num">均分</th>
      <th class="num">差评率</th><th></th></tr></thead><tbody>{drows}</tbody></table></div>
  <div class="card"><h2>高风险版本 TOP10</h2>
    <table><thead><tr><th>版本</th><th class="num">样本</th><th class="num">均分</th>
      <th class="num">差评率</th><th></th></tr></thead><tbody>{vrows}</tbody></table></div>
</div>

<div class="grid g2">
  <div class="card"><h2>代表性差评原声</h2>{quotes}</div>
  <div class="card"><h2>重复文本聚集（刷评 / 模板化）</h2>
    <table><thead><tr><th>文本样例</th><th class="num">重复次数</th>
      <th class="num">均分</th></tr></thead><tbody>{dups}</tbody></table>
    <div class="note">重复文本可能来自自然套话、模板化反馈或异常行为；请结合评分、时间分布、账号与设备信号人工复核，不应仅凭重复次数定性。</div></div>
</div>

<footer>由 analyze.py 生成 · 确定性统计，未使用 LLM · 可复现</footer>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="report.json")
    ap.add_argument("--out", default="review_report.html")
    ap.add_argument("--package", default="", help="兼容没有 package 元数据的旧报告")
    args = ap.parse_args()
    with open(args.report, encoding="utf-8") as fh:
        r = json.load(fh)
    if args.package and not r.get("meta", {}).get("package"):
        r.setdefault("meta", {})["package"] = args.package
    html = build(r)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"已写出 {args.out}（{len(html):,} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
