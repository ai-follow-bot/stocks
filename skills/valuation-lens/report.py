"""valuation-lens 报告渲染（Markdown）

报告结构强调"稀缺 + 前瞻 + 供需 > 当前 PE"的方法论：
PE 单独成列但带"PE 处理"说明，明确每只标的 PE 是 re-rating 合理代价 / 价值陷阱信号 / 锦上添花。
"""

import json
from typing import Optional

from chain_agent import config


def _with_disclaimer(text: str) -> str:
    if config.DISCLAIMER_TEXT in text:
        return text
    return text + "\n\n---\n\n> **免责声明：** " + config.DISCLAIMER_TEXT


def _fmt(v, dash="-"):
    return dash if v is None else v


def _role_badge(role: str) -> str:
    m = {
        "scarce_bottleneck": "稀缺卡脖子",
        "forward_rerating": "前瞻re-rating",
        "supply_demand_play": "供需受益",
        "expensive_but_scarce": "高PE合理",
        "cheap_but_weak": "价值陷阱嫌疑",
        "balanced": "均衡",
    }
    return m.get(role, role or "-")


def render_chain_report(result: dict) -> str:
    """chain / codes 模式 Markdown 报告。"""
    if "error" in result:
        return f"❌ 失败: {result['error']}"

    chain_name = result.get("chain_name", "")
    scoring = result.get("scoring", {}) or {}
    candidates = scoring.get("candidates") or []
    data_quality = result.get("data_quality", "ok")
    search_stats = result.get("search_stats", {})

    lines = [
        f"# {chain_name} 估值镜头（稀缺 + 前瞻 + 供需）",
        f"\n*生成时间: {result.get('run_time')} | 财联回看近 {result.get('days')} 天，Tavily 为当年全网搜索*",
        "\n> **方法论**：稀缺 / 前瞻 / 供需 是主驱动，当前 PE 仅作辅助确认。"
        "高 PE 不否决稀缺+前瞻强的标的（re-rating 合理）；低 PE 不救三信号弱的标的（价值陷阱嫌疑）。\n",
    ]
    if data_quality == "degraded":
        lines.append("> ⚠️ **数据降级**：搜索源失败或 LLM 未返回三维分，本次评分置信度低（详见数据缺口段）。\n")

    # 整体供需分析（LLM 前置文本）
    sda = scoring.get("supply_demand_analysis", "")
    if sda:
        lines.append("## 1. 产业链供需概览（LLM）\n")
        lines.append(sda)

    # 估值排序表
    sec = 2 if sda else 1
    if candidates:
        lines.append(f"\n## {sec}. 估值排序\n")
        lines.append("| 标的 | 稀缺 | 前瞻 | 供需 | 当前PE | PE处理 | 估值分 | 角色 | 逻辑 |")
        lines.append("|------|------|------|------|--------|--------|--------|------|------|")
        for c in candidates:
            sc = c.get("scarcity") or {}
            fw = c.get("forward") or {}
            sd = c.get("supply_demand") or {}
            pe = _fmt(c.get("pe"))
            name = c.get("company") or c.get("name", "")
            lines.append(
                f"| {name} | "
                f"{_fmt(sc.get('score'), '?')} | {_fmt(fw.get('score'), '?')} | "
                f"{_fmt(sd.get('score'), '?')} | {pe} | {c.get('pe_treatment','-')} | "
                f"**{c.get('valuation_score','-')}** | {_role_badge(c.get('role',''))} | "
                f"{c.get('thesis','')} |"
            )

        # 详情
        lines.append("\n### 估值明细")
        for c in candidates:
            sc = c.get("scarcity") or {}
            fw = c.get("forward") or {}
            sd = c.get("supply_demand") or {}
            pe_ctx = c.get("pe_context") or {}
            name = c.get("company") or c.get("name", "")
            lines.append(
                f"\n**{name}— "
                f"估值分 {c.get('valuation_score','-')} · {_role_badge(c.get('role',''))}**"
            )
            lines.append(f"- 稀缺: {sc.get('score','?')} — {sc.get('reason','')}")
            lines.append(f"- 前瞻: {fw.get('score','?')} — {fw.get('reason','')}")
            lines.append(f"- 供需: {sd.get('score','?')} — {sd.get('reason','')}")
            lines.append(f"- PE: {pe_ctx.get('pe','-')}（{pe_ctx.get('verdict','-')}）"
                         f" — {c.get('pe_treatment','')}；{pe_ctx.get('note','')}")
            src_tags = []
            if c.get("source") == "archive":
                src_tags.append("档案召回（24h内复用档案+财联社实时）")
            elif c.get("used_archive"):
                src_tags.append("24h内已搜过（Tavily复用档案+财联社实时）")
            elif c.get("mention_count"):
                src_tags.append(f"财联社热度发现（{c['mention_count']}次提及）")
            pv = c.get("prev_score") or {}
            if pv.get("val") is not None:
                src_tags.append(f"走势: 前次 {pv.get('val')} → 本次 {c.get('valuation_score','-')}")
            if src_tags:
                lines.append(f"- 来源/走势: {' | '.join(src_tags)}")
            risks = c.get("key_risks") or []
            if risks:
                lines.append(f"- 风险: {'; '.join(risks)}")

    # 方法论说明
    sec += 1
    lines.append(f"\n## {sec}. 方法论说明：PE 的角色\n")
    lines.append(
        "本报告对 PE 的处理遵循方向约束（在打分阶段确定性执行，非 LLM 主观）：\n"
        "- **PE 方向（low/neutral/high）由代码基于当批候选 PE 分布确定性计算**（非 LLM 主观，避免 run-to-run 摆动；verdict 相对当批候选集，同票在不同批次里可能 low/neutral/high 不同）。\n"
        "- **三信号均强（高稀缺+高需求+高增长，「三高」标的）** 时，PE **完全不参与调整**——估值由三信号决定。\n"
        "- **稀缺或前瞻强** 时，PE 偏高 **不扣分**——re-rating 的合理代价；PE 偏低额外加分（锦上添花）。\n"
        "- **三信号均弱** 时，PE 偏低 **不加分**——价值陷阱嫌疑；PE 偏高反而扣分。\n"
        "- 其余情形：PE 偏低小加分，偏高小扣分。\n"
        "- 档位之间用**软阈值**过渡（65-70 三高、70-75 strong、45-50 weak），避免分数擦边翻转。\n\n"
        "估值分 = (0.35×稀缺 + 0.30×前瞻 + 0.25×供需)/0.9 + PE方向调整（权重归一到 base 0-100，PE调整 ±10；三高标的调整=0）。\n"
        "即：低 PE 只有在稀缺/前瞻/供需至少有一项站得住时才构成加分；"
        "高 PE 在稀缺+前瞻强时不会被否决；三高标的 PE 完全不参与。"
    )

    # 数据缺口
    sec += 1
    lines.append(f"\n## {sec}. 数据缺口\n")
    gaps = []
    if data_quality == "degraded":
        zero_ev_all = bool(search_stats) and all(
            (d or {}).get("evidence_count", 0) == 0 for d in search_stats.values())
        llm_failed = scoring.get("llm_failed_count") or 0
        if zero_ev_all:
            gaps.append("- ⚠️ 所有搜索源（Tavily+智谱）失败，评分基于 LLM 知识估算")
        if llm_failed:
            gaps.append(f"- ⚠️ {llm_failed}/{len(candidates)} 只候选 LLM 未返回三维分数（降级，置信度低）")
        if not zero_ev_all and not llm_failed:
            gaps.append("- ⚠️ 数据降级（具体原因未知）")
    zero_ev = [code for code, d in search_stats.items()
               if (d or {}).get("evidence_count", 0) == 0]
    if zero_ev:
        gaps.append(f"- {len(zero_ev)}/{len(search_stats)} 只候选 S/F/D 搜索 evidence 为 0")
    if candidates:
        pe_null = sum(1 for c in candidates if c.get("pe") is None)
        if pe_null:
            gaps.append(f"- {pe_null}/{len(candidates)} 只候选 PE 缺失（PE 方向调整退化为中性/小幅）")
        no_score = [c for c in candidates
                    if not all((c.get(k) or {}).get("score") is not None
                               for k in ("scarcity", "forward", "supply_demand"))]
        if no_score:
            gaps.append(f"- {len(no_score)}/{len(candidates)} 只候选 LLM 未返回完整三维分数（详情显示'?'）")
    if not gaps:
        gaps.append("- 暂无明显缺口")
    lines.extend(gaps)

    return _with_disclaimer("\n".join(lines))


def render_stock_verdict(result: dict) -> str:
    """stock 模式 Markdown 报告。"""
    if "error" in result:
        return f"❌ 失败: {result['error']}" + (
            f"\n\nLLM 原文: {result.get('raw_llm','')}" if result.get("raw_llm") else ""
        )

    info = result.get("company_info", {})
    single = result.get("scoring", {}) or {}
    verdict_md = result.get("verdict_md", "")
    data_quality = result.get("data_quality", "ok")

    lines = [
        f"# {result.get('stock_name')}（{result.get('stock_code')}）估值判断",
        f"\n*生成时间: {result.get('run_time')}*",
        "\n> **方法论**：稀缺 / 前瞻 / 供需 是主驱动，当前 PE 仅作辅助确认。\n",
    ]
    if data_quality == "degraded":
        lines.append("> ⚠️ **数据降级**：搜索源失败或 LLM 未返回三维分，本次判断置信度低。\n")

    lines.append("## 公司定位\n")
    lines.append(f"- 主营业务: {info.get('business','-')}")
    lines.append(f"- 所属产业链: {info.get('chain_name','-')}")
    lines.append(f"- 所处环节: {info.get('segment','-')}")

    # 三维估值框
    if single:
        sc = single.get("scarcity") or {}
        fw = single.get("forward") or {}
        sd = single.get("supply_demand") or {}
        pe_ctx = single.get("pe_context") or {}
        lines.append("\n## 三维估值打分\n")
        lines.append("| 维度 | 分数 | 理由 |")
        lines.append("|------|------|------|")
        lines.append(f"| 稀缺 | {sc.get('score','?')} | {sc.get('reason','')} |")
        lines.append(f"| 前瞻 | {fw.get('score','?')} | {fw.get('reason','')} |")
        lines.append(f"| 供需 | {sd.get('score','?')} | {sd.get('reason','')} |")
        lines.append(
            f"\n**估值分: {single.get('valuation_score','-')} · "
            f"{_role_badge(single.get('role',''))}**\n"
            f"- PE: {pe_ctx.get('pe','-')}（{pe_ctx.get('verdict','-')}）— {single.get('pe_treatment','')}；{pe_ctx.get('note','')}\n"
            f"- 逻辑: {single.get('thesis','')}"
        )

    lines.append("\n## LLM 估值判断\n")
    lines.append(verdict_md)

    return _with_disclaimer("\n".join(lines))
