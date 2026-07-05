"""deep-analyze 报告渲染（Markdown）"""

import json
from typing import Optional

from chain_agent import config


def _with_disclaimer(text: str) -> str:
    """在 Markdown 文本末尾追加免责声明（避免重复）。"""
    if config.DISCLAIMER_TEXT in text:
        return text
    return text + "\n\n---\n\n> **免责声明：** " + config.DISCLAIMER_TEXT


def _bottleneck_badge(score) -> str:
    try:
        s = int(score)
    except Exception:
        return ""
    return "⚠️ 卡脖子" if s >= 14 else ""


def render_chain_report(result: dict) -> str:
    """chain 模式 Markdown 报告"""
    chain = result.get("chain", {})
    bottleneck = result.get("bottleneck", {})
    scoring = result.get("scoring", {})
    search_stats = result.get("search_stats", {})
    data_quality = result.get("data_quality", "ok")

    lines = [
        f"# {chain.get('chain_name', result.get('chain_name', ''))} 产业链深度拆解",
        f"\n*生成时间: {result.get('run_time')} | 数据窗口: {result.get('days')} 天*",
    ]
    if data_quality == "degraded":
        lines.append("\n> ⚠️ **数据降级**：所有搜索源（Tavily+智谱+akshare）均失败，"
                     "本次评分基于 LLM 知识估算，置信度低。\n")
    lines.append("\n## 1. 产业链结构\n")
    lines.append("| 环节 | 上下游 | 全球龙头 | 国内龙头 | 集中度 | 国产化率 | 技术门槛 |")
    lines.append("|------|--------|---------|---------|--------|---------|---------|")
    for s in chain.get("segments", []):
        lines.append(
            f"| {s.get('name','')} | {s.get('role','')} | "
            f"{', '.join(s.get('global_leaders', [])[:2]) or '-'} | "
            f"{', '.join(s.get('cn_leaders', [])[:2]) or '-'} | "
            f"{s.get('concentration','-')} | {s.get('cn_share','-')} | "
            f"{s.get('tech_barrier','-')} |"
        )

    # 卡脖子表
    bn_segments = bottleneck.get("segments", [])
    if bn_segments:
        lines.append("\n## 2. 卡脖子分析\n")
        lines.append("| 环节 | 集中度 | 国替空间 | 技术门槛 | 价格信号 | 总分 | 卡脖子? | 提取数字 |")
        lines.append("|------|--------|---------|---------|---------|------|---------|---------|")
        for s in bn_segments:
            nums = s.get("extracted_numbers") or {}
            nums_str = ""
            if nums.get("cr3"):
                nums_str += f"CR3={nums['cr3']} "
            if nums.get("cn_share"):
                nums_str += f"国替率={nums['cn_share']}"
            nums_str = nums_str.strip() or "-"
            lines.append(
                f"| {s.get('name','')} | {s.get('supply_concentration','-')} | "
                f"{s.get('cn_substitution_room','-')} | {s.get('tech_barrier','-')} | "
                f"{s.get('price_signal','-')} | {s.get('bottleneck_score','-')} | "
                f"{'⚠️' if s.get('is_bottleneck') else ''} | {nums_str} |"
            )
        top_bn = bottleneck.get("top_bottlenecks", [])
        if top_bn:
            lines.append(f"\n**Top 卡脖子环节**: {', '.join(top_bn)}\n")
        # reasoning（含 evidence 引用）
        lines.append("**环节判断理由**（含 evidence 引用）:")
        for s in bn_segments:
            if s.get("reasoning"):
                ev_ids = s.get("evidence_ids") or []
                ev_str = f" `[evidence: {','.join(ev_ids)}]`" if ev_ids else ""
                lines.append(f"- {s['name']}{ev_str}: {s['reasoning']}")

    # 三维评分
    candidates = scoring.get("candidates") or scoring.get("segments") or []
    supply_demand_analysis = scoring.get("supply_demand_analysis", "")
    if supply_demand_analysis:
        lines.append("\n## 3. 产业链供需分析\n")
        lines.append(supply_demand_analysis)
    if candidates:
        lines.append(f"\n## {4 if supply_demand_analysis else 3}. 三维评分（候选标的）\n")
        lines.append("| 标的 | 代码 | 环节 | 供需(30) | 国替(30) | 业绩(40) | 总分 | 权重 | PE | 市值(亿) |")
        lines.append("|------|------|------|---------|---------|---------|------|------|-----|---------|")
        # 按 total_score 排序
        def _total(c):
            sc = c.get("scores", {}) if isinstance(c.get("scores"), dict) else {}
            return sc.get("total_score", c.get("total_score", 0)) or 0
        candidates_sorted = sorted(candidates, key=_total, reverse=True)
        for c in candidates_sorted:
            sc = c.get("scores", {}) if isinstance(c.get("scores"), dict) else {}
            pe = c.get("pe") if c.get("pe") is not None else "-"
            mktcap = c.get("market_cap") if c.get("market_cap") is not None else "-"
            lines.append(
                f"| {c.get('company', c.get('name',''))} | {c.get('stock_code', c.get('code',''))} | "
                f"{c.get('segment','-')} | {sc.get('supply_demand','-')} | "
                f"{sc.get('domestic_substitution','-')} | {sc.get('earnings_realization','-')} | "
                f"{_total(c)} | {c.get('weight','-')} | {pe} | {mktcap} |"
            )
        # 详情
        lines.append("\n### 评分明细")
        for c in candidates_sorted:
            sc = c.get("scores", {}) if isinstance(c.get("scores"), dict) else {}
            rat = c.get("rationale", {}) if isinstance(c.get("rationale"), dict) else {}
            lines.append(f"\n**{c.get('company', c.get('name',''))}（{c.get('stock_code', c.get('code',''))}）— 总分 {_total(c)}**")
            if rat.get("supply_demand_reason"):
                lines.append(f"- 供需: {sc.get('supply_demand','-')} — {rat['supply_demand_reason']}")
            if rat.get("domestic_substitution_reason"):
                lines.append(f"- 国替: {sc.get('domestic_substitution','-')} — {rat['domestic_substitution_reason']}")
            if rat.get("earnings_realization_reason"):
                lines.append(f"- 业绩: {sc.get('earnings_realization','-')} — {rat['earnings_realization_reason']}")
            risks = c.get("key_risks", [])
            if risks:
                lines.append(f"- 风险: {'; '.join(risks)}")

    # 数据缺口
    section_num = 5 if supply_demand_analysis else 4
    lines.append(f"\n## {section_num}. 数据缺口\n")
    gaps = []
    if data_quality == "degraded":
        gaps.append("- ⚠️ 所有搜索源（Tavily+智谱+akshare）失败，评分基于 LLM 知识估算")
    if not any(d.get("tavily_count", 0) > 0 for d in search_stats.values()):
        if data_quality != "degraded":
            gaps.append("- 网络搜索不可用（Tavily+智谱均失败），所有环节缺深度搜索数据")
    low_news = [seg for seg, d in search_stats.items() if d.get("akshare_news_count", 0) == 0]
    if low_news:
        gaps.append(f"- 以下环节 akshare 新闻命中为 0: {', '.join(low_news)}")
    if not gaps:
        gaps.append("- 暂无明显缺口")
    lines.extend(gaps)

    return _with_disclaimer("\n".join(lines))


def render_stock_verdict(result: dict) -> str:
    """stock 模式 Markdown 报告"""
    # 漏洞 2 配套：error 时直接返回失败信息
    if "error" in result:
        return f"❌ 失败: {result['error']}" + (
            f"\n\nLLM 原文: {result.get('raw_llm','')}" if result.get("raw_llm") else ""
        )

    info = result.get("company_info", {})
    chain_result = result.get("chain_analysis", {})
    verdict_md = result.get("verdict_md", "")
    data_quality = result.get("data_quality", "ok")

    lines = [
        f"# {result.get('stock_name')}（{result.get('stock_code')}）投资判断",
        f"\n*生成时间: {result.get('run_time')}*",
    ]
    if data_quality == "degraded":
        lines.append("\n> ⚠️ **数据降级**：所有搜索源失败，本次判断基于 LLM 知识估算，置信度低。\n")
    lines.append("\n## 公司定位\n")
    lines.append(f"- 主营业务: {info.get('business','-')}")
    lines.append(f"- 所属产业链: {info.get('chain_name','-')}")
    lines.append(f"- 所处环节: {info.get('segment','-')} ({info.get('role','-')})")
    lines.append("")
    lines.append("## LLM 综合判断\n")
    lines.append(verdict_md)
    lines.append("")
    lines.append("---")
    lines.append("## 附：所属产业链分析\n")
    lines.append(render_chain_report(chain_result) if chain_result else "(产业链分析失败)")
    return _with_disclaimer("\n".join(lines))
