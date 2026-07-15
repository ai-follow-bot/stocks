"""
财联社新闻数据挖掘 — 可视化报告生成器

用法:
    .venv/bin/python scripts/visualize_cailianshe.py
    .venv/bin/python scripts/visualize_cailianshe.py --days 30
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# ===== 数据加载 =====
def load_news_for_date(date_str: str) -> list[dict]:
    p = Path(f"/root/.hermes/data/investment-research/news/{date_str}/news_{date_str}.json")
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    return data.get("news", [])


def get_all_dates(max_days: int = None):
    hermes_dir = Path("/root/.hermes/data/investment-research/news")
    dates = sorted(d.name for d in hermes_dir.iterdir() if d.is_dir())
    if max_days:
        dates = dates[-max_days:]
    return dates


def load_ecosystem():
    p = Path("/opt/stocks/data/sector_ecosystem.json")
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if k != "metadata"}


def load_keywords():
    p = Path("/opt/stocks/data/sector_keywords.json")
    data = json.loads(p.read_text())
    return data.get("sectors", {})


# ===== 分析函数 =====
def build_series(dates: list[str], ecosystem: dict, keywords: dict):
    """构建各板块的新闻量时间序列"""
    # sector -> date -> count
    sector_daily: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    daily_totals: dict[str, int] = {}
    daily_levels: dict[str, dict[str, int]] = {}
    sector_total_hits: Counter = Counter()

    # 构建关键词反向索引
    kw_to_sectors: dict[str, list[str]] = {}
    for sk, kw_list in keywords.items():
        if not isinstance(kw_list, list):
            continue
        for kw in kw_list:
            kw_lower = kw.lower().strip()
            if kw_lower:
                kw_to_sectors.setdefault(kw_lower, []).append(sk)

    for date_str in dates:
        news = load_news_for_date(date_str)
        daily_totals[date_str] = len(news)

        # 等级分布
        lv = Counter(n.get("level", "?") for n in news)
        daily_levels[date_str] = {"A": lv.get("A", 0), "B": lv.get("B", 0), "C": lv.get("C", 0)}

        # 板块匹配
        for n in news:
            text = (n.get("title", "") + " " + n.get("content", "")).lower()
            matched = set()
            for kw, sectors in kw_to_sectors.items():
                if kw in text:
                    for sk in sectors:
                        matched.add(sk)
            for sk in matched:
                sector_daily[sk][date_str] = sector_daily[sk].get(date_str, 0) + 1
                sector_total_hits[sk] += 1

    return sector_daily, daily_totals, daily_levels, sector_total_hits, kw_to_sectors


# ===== 生成 HTML =====
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>财联社新闻数据挖掘</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f172a;color:#e2e8f0;font-family:-apple-system,'Noto Sans SC',sans-serif;padding:24px}
.header{max-width:1400px;margin:0 auto 24px}
.header h1{font-size:28px;font-weight:700}
.header .sub{color:#94a3b8;font-size:14px;margin-top:6px}
.header .stats{display:flex;gap:16px;margin-top:16px;flex-wrap:wrap}
.stat-tile{background:#1e293b;border-radius:10px;padding:14px 20px;min-width:120px}
.stat-tile .num{font-size:26px;font-weight:700;color:#38bdf8}
.stat-tile .label{font-size:12px;color:#94a3b8;margin-top:2px}
.grid{max-width:1400px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#1e293b;border-radius:12px;padding:20px;overflow:hidden}
.card h2{font-size:15px;font-weight:600;margin-bottom:10px;color:#f1f5f9}
.card-full{grid-column:1/-1}
.chart{width:100%;height:360px}
.chart-sm{height:260px}
.footer{max-width:1400px;margin:24px auto 0;font-size:12px;color:#475569;text-align:center}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
<h1>📊 财联社新闻数据挖掘</h1>
<div class="sub">基于 {{TOTAL_DAYS }} 天新闻数据 · 共 {{TOTAL_NEWS }} 条 · 覆盖 {{TOTAL_SECTORS }} 个板块</div>
<div class="stats" id="statTiles"></div>
</div>
<div class="grid">
<div class="card card-full"><h2>📈 新闻量时间序列</h2><div id="chartTimeseries" class="chart"></div></div>
<div class="card"><h2>🏷️ 板块关键词命中 TOP15</h2><div id="chartSectors" class="chart chart-sm"></div></div>
<div class="card"><h2>📋 新闻等级分布</h2><div id="chartLevels" class="chart chart-sm"></div></div>
<div class="card card-full"><h2>🔥 板块-新闻量热力图（日维度·TOP10板块）</h2><div id="chartHeatmap" class="chart" style="height:500px"></div></div>
<div class="card card-full"><h2>📅 板块新闻量趋势（TOP8）</h2><div id="chartTrend" class="chart" style="height:420px"></div></div>
</div>
<div class="footer">数据来源：财联社 · 关键词映射到 A 股 30 个板块</div>

<script>
// ===== 数据 =====
const DATA = {{DATA_JSON }};
const ECO_NAMES = {{ECO_NAMES }};
const COLORS = ['#38bdf8','#f472b6','#34d399','#fbbf24','#a78bfa','#fb923c','#2dd4bf','#f87171','#818cf8','#4ade80','#e879f9','#facc15','#22d3ee','#a3a3a3','#60a5fa'];

// ===== 统计概览 =====
const totalNews = Object.values(DATA.dailyTotals).reduce((a,b)=>a+b,0);
document.getElementById('statTiles').innerHTML = [
  ['总天数', Object.keys(DATA.dailyTotals).length],
  ['总新闻', totalNews.toLocaleString()],
  ['日均', Math.round(totalNews/Object.keys(DATA.dailyTotals).length).toLocaleString()],
  ['板块数', Object.keys(DATA.sectorTotal).length],
].map(([l,n]) => `<div class="stat-tile"><div class="num">${n}</div><div class="label">${l}</div></div>`).join('');

// ===== 1. 新闻量时间序列 =====
(function(){
  const dates = Object.keys(DATA.dailyTotals);
  const vals = dates.map(d => DATA.dailyTotals[d]);
  const lvA = dates.map(d => DATA.dailyLevels[d]?.A||0);
  const lvB = dates.map(d => DATA.dailyLevels[d]?.B||0);
  const c = echarts.init(document.getElementById('chartTimeseries'));
  c.setOption({
    tooltip:{trigger:'axis'},
    grid:{left:50,right:16,bottom:30,top:10},
    xAxis:{type:'category',data:dates,axisLabel:{fontSize:10,rotate:45}},
    yAxis:{type:'value'},
    series:[
      {name:'C级',type:'bar',stack:'total',data:vals.map((v,i)=>v-lvA[i]-lvB[i]),itemStyle:{color:'#334155'}},
      {name:'B级',type:'bar',stack:'total',data:lvB,itemStyle:{color:'#60a5fa'}},
      {name:'A级',type:'bar',stack:'total',data:lvA,itemStyle:{color:'#fbbf24'}},
      {name:'合计',type:'line',data:vals,lineStyle:{width:2,color:'#38bdf8'},symbol:'none'},
    ]
  });
  window.addEventListener('resize',()=>c.resize());
})();

// ===== 2. 板块关键词命中 TOP15 =====
(function(){
  const sorted = Object.entries(DATA.sectorTotal).sort((a,b)=>b[1]-a[1]).slice(0,15);
  const c = echarts.init(document.getElementById('chartSectors'));
  c.setOption({
    tooltip:{trigger:'axis'},
    grid:{left:100,right:16,bottom:16,top:10},
    xAxis:{type:'value'},
    yAxis:{type:'category',data:sorted.map(([k])=>ECO_NAMES[k]||k).reverse(),axisLabel:{fontSize:11}},
    series:[{
      type:'bar',data:sorted.map(([,v])=>v).reverse(),
      itemStyle:{color:(p)=>COLORS[p.dataIndex%COLORS.length],borderRadius:[0,4,4,0]},
      barMaxWidth:18,
    }]
  });
  window.addEventListener('resize',()=>c.resize());
})();

// ===== 3. 新闻等级分布 =====
(function(){
  const a = Object.values(DATA.dailyLevels).reduce((s,d)=>s+d.A,0);
  const b = Object.values(DATA.dailyLevels).reduce((s,d)=>s+d.B,0);
  const c = Object.values(DATA.dailyLevels).reduce((s,d)=>s+d.C,0);
  const chart = echarts.init(document.getElementById('chartLevels'));
  chart.setOption({
    tooltip:{formatter:'{b}: {c} ({d}%)'},
    series:[{
      type:'pie',radius:['30%','70%'],center:['50%','55%'],
      data:[
        {name:'A级',value:a,itemStyle:{color:'#fbbf24'}},
        {name:'B级',value:b,itemStyle:{color:'#60a5fa'}},
        {name:'C级',value:c,itemStyle:{color:'#334155'}},
      ],
      label:{color:'#94a3b8',fontSize:12},
    }]
  });
  window.addEventListener('resize',()=>chart.resize());
})();

// ===== 4. 板块-新闻量热力图（TOP10板块） =====
(function(){
  const top = Object.entries(DATA.sectorTotal).sort((a,b)=>b[1]-a[1]).slice(0,10).map(([k])=>k);
  const dates = Object.keys(DATA.dailyTotals);
  const cellData=[];
  for(let di=0;di<dates.length;di++){
    for(let si=0;si<top.length;si++){
      const v=DATA.sectorDaily[top[si]]?.[dates[di]]||0;
      if(v>0)cellData.push([di,si,v]);
    }
  }
  const maxV=Math.max(...cellData.map(d=>d[2]),1);
  const chart=echarts.init(document.getElementById('chartHeatmap'));
  chart.setOption({
    tooltip:{position:'top',formatter:(p)=>`${ECO_NAMES[top[p.value[1]]]||top[p.value[1]]}<br/>${dates[p.value[0]]}: ${p.value[2]}条`},
    grid:{left:90,right:60,bottom:50,top:10},
    xAxis:{type:'category',data:dates,splitArea:{show:true},axisLabel:{fontSize:9,rotate:45},show:dates.length<=60},
    yAxis:{type:'category',data:top.map(k=>ECO_NAMES[k]||k).reverse(),splitArea:{show:true},axisLabel:{fontSize:11}},
    visualMap:{min:0,max:maxV,calculable:true,inRange:{color:['#1e293b','#1d4ed8','#38bdf8','#fbbf24','#f87171']},orient:'vertical',right:8,top:20,bottom:20,textStyle:{color:'#94a3b8'}},
    series:[{type:'heatmap',data:cellData,label:{show:false},emphasis:{itemStyle:{shadowBlur:10,shadowColor:'rgba(0,0,0,0.5)'}}}]
  });
  window.addEventListener('resize',()=>chart.resize());
})();

// ===== 5. 板块新闻量趋势（TOP8） =====
(function(){
  const top=Object.entries(DATA.sectorTotal).sort((a,b)=>b[1]-a[1]).slice(0,8).map(([k])=>k);
  const dates=Object.keys(DATA.dailyTotals);
  const series=top.map((sk,i)=>({
    name:ECO_NAMES[sk]||sk,
    type:'line',smooth:true,symbol:'circle',symbolSize:4,
    lineStyle:{width:2},
    data:dates.map(d=>DATA.sectorDaily[sk]?.[d]||0),
    itemStyle:{color:COLORS[i]},
  }));
  const chart=echarts.init(document.getElementById('chartTrend'));
  chart.setOption({
    tooltip:{trigger:'axis'},
    legend:{data:series.map(s=>s.name),textStyle:{color:'#94a3b8',fontSize:11},bottom:0},
    grid:{left:45,right:16,bottom:50,top:10},
    xAxis:{type:'category',data:dates,axisLabel:{fontSize:9,rotate:45}},
    yAxis:{type:'value',name:'新闻条数',nameTextStyle:{fontSize:11,color:'#94a3b8'}},
    series,
  });
  window.addEventListener('resize',()=>chart.resize());
})();
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="财联社新闻数据挖掘可视化")
    parser.add_argument("--days", type=int, default=60, help="回溯天数")
    parser.add_argument("--out", default=str(OUTPUT_DIR / "cailianshe_viz.html"))
    args = parser.parse_args()

    dates = get_all_dates(args.days)
    ecosystem = load_ecosystem()
    keywords = load_keywords()

    print(f"[viz] 加载 {len(dates)} 天数据...", file=sys.stderr)
    sector_daily, daily_totals, daily_levels, sector_total_hits, kw_to_sectors = build_series(dates, ecosystem, keywords)

    eco_names = {sk: info.get("name", sk) for sk, info in ecosystem.items()}

    total_news = sum(daily_totals.values())
    print(f"[viz] 总新闻: {total_news}, 板块命中: {len(sector_total_hits)}", file=sys.stderr)

    data_json = json.dumps({
        "dailyTotals": dict(daily_totals),
        "dailyLevels": daily_levels,
        "sectorTotal": dict(sector_total_hits.most_common()),
        "sectorDaily": {k: dict(v) for k, v in sector_daily.items()},
    }, ensure_ascii=False)

    html = (
        HTML_TEMPLATE
        .replace("{{TOTAL_DAYS }}", str(len(dates)))
        .replace("{{TOTAL_NEWS }}", str(total_news))
        .replace("{{TOTAL_SECTORS }}", str(len(sector_total_hits)))
        .replace("{{DATA_JSON }}", data_json)
        .replace("{{ECO_NAMES }}", json.dumps(eco_names, ensure_ascii=False))
    )

    Path(args.out).write_text(html, encoding="utf-8")
    print(f"[viz] ✅ 已生成: {args.out}", file=sys.stderr)

    # 同时输出 JSON 数据文件（供后台 API 读取）
    json_out = {
        "dates": dates,
        "eco_names": eco_names,
        "total_news": total_news,
        "dailyTotals": dict(daily_totals),
        "dailyLevels": daily_levels,
        "sectorTotal": dict(sector_total_hits.most_common()),
        "sectorDaily": {k: dict(v) for k, v in sector_daily.items()},
    }
    json_path = Path(args.out).with_suffix(".json")
    json_path.write_text(json.dumps(json_out, ensure_ascii=False), encoding="utf-8")
    print(f"[viz] ✅ JSON 数据已保存: {json_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
