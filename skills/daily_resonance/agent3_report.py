"""
Agent 3: 每日共振简报生成

将Agent 2的共振排行榜 + 关联新闻原文 → LLM综合研判 → Markdown报告。
"""
import json
import sys
from datetime import datetime
from typing import Optional

from .config import (
    LLM_PROVIDER, LLM_MODEL, LLM_MAX_TOKENS, LLM_TEMPERATURE,
    SKILL_DIR,
)


def generate_report(
    resonance_list: list[dict],
    sector_events: dict,
    date: str,
    use_llm: bool = True,
) -> str:
    """
    生成每日共振简报。

    参数:
        resonance_list: Agent 2的输出（排序后的板块共振列表）
        sector_events: Agent 1的输出（原始事件映射）
        date: 日期字符串 "YYYY-MM-DD"
        use_llm: 是否使用LLM生成报告（False则生成模板报告）

    返回:
        Markdown格式的简报
    """
    if use_llm:
        try:
            return _llm_report(resonance_list, sector_events, date)
        except Exception as e:
            print(f"[Agent 3] LLM生成失败: {e}，回退到模板报告",
                  file=sys.stderr)
            return _template_report(resonance_list, date)

    return _template_report(resonance_list, date)


def _template_report(resonance_list: list[dict], date: str) -> str:
    """模板报告（无需LLM）"""
    top5 = resonance_list[:5]

    lines = []
    lines.append(f"# 每日板块共振简报 — {date}")
    lines.append("")
    lines.append("> 自动生成 | 数据来源: 财联社 | 共振模型: 5维加权")
    lines.append("")

    # 总览
    lines.append("## 共振总览")
    lines.append("")
    if top5:
        top = top5[0]
        lines.append(f"**最强共振**: {top['name']} — {top['score']}分")
        lines.append(f"**事件数**: {top['stats']['total_events']}条 | "
                     f"**情绪**: 正面{top['stats']['positive']} / 负面{top['stats']['negative']}")
        lines.append(f"**事件类型**: {', '.join(top['stats']['event_types'])}")
    lines.append("")

    # TOP5 详细
    lines.append("## 板块共振排行榜")
    lines.append("")
    lines.append("| 排名 | 板块 | 共振分数 | 事件数 | 情绪 | 产业链共振 | 事件多样性 |")
    lines.append("|------|------|---------|--------|------|-----------|-----------|")
    for i, r in enumerate(top5, 1):
        dim = r['dimensions']
        sentiment_str = f"P{r['stats']['positive']}/N{r['stats']['negative']}"
        lines.append(
            f"| {i} | {r['name']} | {r['score']} | {r['stats']['total_events']} | "
            f"{sentiment_str} | {dim['chain_resonance']:.2f} | "
            f"{len(r['stats']['event_types'])}种 |"
        )
    lines.append("")

    # 深度分析
    lines.append("## TOP3 深度分析")
    lines.append("")
    for i, r in enumerate(top5[:3], 1):
        dim = r['dimensions']
        lines.append(f"### {i}. {r['name']} — {r['score']}分")
        lines.append("")
        lines.append(f"- **事件密度**: {dim['density']:.2f}")
        lines.append(f"- **情绪强度**: {dim['sentiment']:.2f}")
        lines.append(f"- **产业链共振**: {dim['chain_resonance']:.2f}")
        lines.append(f"- **事件多样性**: {dim['diversity']:.2f}")
        lines.append(f"- **重要性加权**: {dim['importance']:.2f}")
        lines.append("")
        # 列出前5条事件
        events = r.get('events', [])
        for ev in events[:5]:
            sentiment_mark = {1: "📈", -1: "📉", 0: "➖"}.get(ev.get('sentiment', 0), "")
            lines.append(f"  - {sentiment_mark} [{ev.get('event_type', 'general')}] "
                        f"{ev.get('title', '')}")
        if len(events) > 5:
            lines.append(f"  - ... 还有 {len(events) - 5} 条事件")
        lines.append("")

    # 风险提示
    lines.append("## 风险提示")
    lines.append("")
    # 检查情绪过热的板块
    for r in top5:
        if r['dimensions']['sentiment'] > 0.8 and r['dimensions']['chain_resonance'] < 0.2:
            lines.append(f"- ⚠️ **{r['name']}**: 情绪偏高但缺乏产业链共振支撑，"
                        f"可能为纯情绪驱动")
    if not any(r['dimensions']['sentiment'] > 0.8 and r['dimensions']['chain_resonance'] < 0.2
               for r in top5):
        lines.append("- 暂未检测到明显风险信号")
    lines.append("")

    # 附录
    lines.append("---")
    lines.append("")
    lines.append("### 方法论")
    lines.append("")
    lines.append("共振分数 = 5维加权（事件密度×0.25 + 情绪强度×0.25 + "
                "产业链共振×0.20 + 事件多样性×0.15 + 重要性加权×0.15）× 100")
    lines.append("")

    return "\n".join(lines)


def _llm_report(
    resonance_list: list[dict],
    sector_events: dict,
    date: str,
) -> str:
    """使用LLM生成深度报告"""
    # 构建输入数据
    top10 = resonance_list[:10]
    top5 = resonance_list[:5]

    # 序列化排行榜数据
    ranking_data = []
    for r in top10:
        ranking_data.append({
            "rank": top10.index(r) + 1,
            "sector": r["name"],
            "score": r["score"],
            "dimensions": r["dimensions"],
            "stats": r["stats"],
        })

    # 收集TOP3的事件原文（截断）
    news_context = []
    for r in top5[:3]:
        events = r.get("events", [])
        for ev in events[:8]:
            news_context.append({
                "sector": r["name"],
                "title": ev.get("title", ""),
                "content_snippet": ev.get("content_snippet", ""),
                "event_type": ev.get("event_type", ""),
                "sentiment": ev.get("sentiment", 0),
            })

    prompt = f"""你是一位专业的A股板块共振分析师。请基于以下数据生成每日共振简报。

## 今日日期
{date}

## 板块共振排行榜TOP10
{json.dumps(ranking_data, ensure_ascii=False, indent=2)}

## TOP3板块关联新闻摘要
{json.dumps(news_context, ensure_ascii=False, indent=2)}

请生成一份Markdown格式的每日共振简报，包含以下内容：

1. **共振总览** - 今日最强共振板块及驱动因素概览（2-3句话）
2. **TOP3深度分析** - 每个板块的：
   - 共振逻辑（哪些维度的数据支撑）
   - 关键事件（引用新闻标题）
   - 持续性判断（短期情绪驱动还是有产业链共振支撑）
3. **风险提示** - 情绪过热、与产业链脱节、或其他值得注意的风险

要求：简洁、专业、数据驱动。每个板块分析不超过5句话。"""

    # 使用chain_agent的LLM客户端
    try:
        from chain_agent.llm.client import llm_synthesize
    except ImportError:
        # 回退到模板
        print("[Agent 3] chain_agent.llm.client 不可用，使用模板",
              file=sys.stderr)
        return _template_report(resonance_list, date)

    # 构建消息
    system_prompt = "你是一位专业的A股板块共振分析师。回答简洁、专业、数据驱动。"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    result = llm_synthesize(
        messages=messages,
        provider=LLM_PROVIDER,
        model=LLM_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        temperature=LLM_TEMPERATURE,
    )

    if not result:
        print("[Agent 3] LLM返回空结果，使用模板", file=sys.stderr)
        return _template_report(resonance_list, date)

    # 包装Markdown
    report = f"# 每日板块共振简报 — {date}\n\n"
    report += "> 🤖 AI生成 | 数据来源: 财联社 | 共振模型: 5维加权 + LLM综合研判\n\n"
    report += result

    return report
