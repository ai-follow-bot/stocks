"""cycle-lens 报告渲染（SPEC §9）。不显示股票代码（只显示名称）。"""

from datetime import datetime


def _fmt_pct(v):
    return f"{v*100:+.1f}%" if isinstance(v, (int, float)) else "N/A"


def _fmt_pe(v):
    return f"{v:.2f}x" if isinstance(v, (int, float)) and v > 0 else "N/A"


def _fmt_pctile(v):
    return f"{v*100:.0f}%" if isinstance(v, (int, float)) else "N/A"


def render_one(name: str, r: dict) -> str:
    """单股报告。"""
    if r.get("error"):
        return f"### {name}\n> ❌ 失败：{r['error']}\n"
    d = r.get("decomp", {}) or {}
    rev = r.get("revision", {}) or {}
    llm = r.get("llm_judgment", {}) or {}
    lines = [f"### {name}"]
    lines.append(f"> 分类：{d.get('classification','N/A')} | "
                 f"PE 分位：{_fmt_pctile(d.get('pe_percentile'))} | "
                 f"EPS 峰值：{d.get('eps_at_peak','N/A')} | "
                 f"predict_EPS {rev.get('revision','N/A')}")

    lines.append(f"\n**驱动分解（确定性）**")
    lines.append(f"- EPS 贡献：{_fmt_pct(d.get('eps_contrib'))} | PE 贡献：{_fmt_pct(d.get('pe_contrib'))}")
    lines.append(f"- 前瞻 PE：{_fmt_pe(d.get('forward_pe'))} | 当前 PE：{_fmt_pe(r.get('current_pe'))} | "
                 f"TTM EPS：{d['current_ttm_eps']:.4f}" if d.get("current_ttm_eps")
                 else f"- 前瞻 PE：{_fmt_pe(d.get('forward_pe'))} | 当前 PE：{_fmt_pe(r.get('current_pe'))}")
    if rev.get("prev") is not None:
        lines.append(f"- predict_EPS：{rev.get('prev')} -> {rev.get('curr')}（{rev.get('revision')}）")

    if llm:
        lines.append(f"\n**三问判断（LLM）**")
        lines.append(f"- 看盈利：{llm.get('profitability','')}")
        lines.append(f"- 看需求：{llm.get('demand','')}")
        lines.append(f"- 看估值：{llm.get('valuation','')}")
        lines.append(f"\n**自我调节定位**")
        lines.append(f"- 8步当前：{llm.get('cycle_stage','')}")
        lines.append(f"\n**警惕信号 + 终极判断**")
        lines.append(f"- 警惕：{llm.get('warning','')}")
        lines.append(f"- 终极：{llm.get('verdict','')}")

    if r.get("data_quality") == "degraded":
        lines.append(f"\n> ⚠️ 数据降级（缺 {', '.join(r.get('missing',[]))}）")
    return "\n".join(lines) + "\n"


def render_report(result: dict) -> str:
    """主报告。result = run_cycle_lens 返回。"""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    if result.get("mode") == "stock":
        r = result.get("result", {})
        name = result.get("name", "")
        lines = [f"# 业绩-估值周期镜头 - {name}", ""]
        lines.append(f"> 框架：股价 = EPS × PE | 分类：业绩型/泡沫型/周期陷阱 | 生成：{now}")
        lines.append("")
        lines.append(render_one(name, r))
        return "\n".join(lines)

    # chain
    sector_name = result.get("sector_name", result.get("sector", ""))
    lines = [f"# 业绩-估值周期镜头 - {sector_name}", ""]
    lines.append(f"> 框架：股价 = EPS × PE | 分类：业绩型/泡沫型/周期陷阱 | 生成：{now}")
    lines.append("")
    summary = result.get("summary", {}) or {}
    if summary:
        lines.append(f"## 板块汇总")
        lines.append(f"- 标的数：{summary.get('count',0)}")
        lines.append(f"- 分类分布：{summary.get('classification_dist',{})}")
        if summary.get("warning_stocks"):
            lines.append(f"- 警惕信号标的：{', '.join(summary['warning_stocks'])}")
        lines.append("")
    lines.append(f"## 个股分析")
    for r in result.get("results", []):
        lines.append(render_one(r.get("name", r.get("code","")), r))
        lines.append("---")
    return "\n".join(lines)
