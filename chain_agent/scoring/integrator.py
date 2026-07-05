"""
Layer 4: 评分整合

合并 overflow + tech_option + heuristic 三套信号，输出最终 0-100 分 + 角色 + rationale。
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from .. import config
from .heuristic import heuristic_score
from .overflow import analyze_overflow
from .tech_option import analyze_tech_options
from .quotes import QuoteProvider, get_quote_provider


def score_candidates(sector: str, discovered: Dict,
                     quote_provider: QuoteProvider = None,
                     tavily_search=None,
                     tavily_results_pool: List[Dict] = None,
                     enriched_extras: Dict[str, Dict] = None) -> Dict:
    """
    对候选标的打分。

    Args:
        tavily_results_pool: 主 pipeline 采集的 Tavily results 列表，
                             传给 tech_option 做 substring 匹配，避免每股二次调用
        enriched_extras: {code: extras_dict} 来自 collectors.stock_data.enrich_candidates()
                         阶段，含 dragon_tiger / fund_flow_120d / margin / research_reports /
                         lockup / ths_topics。用于追加 4 维 scoring bonus。

    Returns:
        {
            "sector", "overflow_strength", "leader_saturation",
            "overflow_raw", "tech_raw",
            "scored": [{code, name, sector, tier, in_pool, news_hits, source,
                        score, role, rationale, extras}]
        }
    """
    sector_under = config.to_under(sector)
    sector_hyphen = config.to_hyphen(sector)

    if quote_provider is None:
        quote_provider = get_quote_provider()

    # 并行跑溢出 + 技术期权分析
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_ov = ex.submit(analyze_overflow, sector, quote_provider)
        fut_tech = ex.submit(
            analyze_tech_options, sector, tavily_search, tavily_results_pool
        )
        overflow = fut_ov.result()
        tech = fut_tech.result()

    # 建立索引
    overflow_by_code = {}
    if not overflow.get("summary", "").startswith("板块"):
        for l in overflow.get("leader_analysis", []):
            overflow_by_code[l["code"]] = {"role": "leader", **l}
        for c in overflow.get("candidates", []):
            overflow_by_code.setdefault(c["code"], {"role": "second_tier", **c})

    tech_by_code = {op["code"]: op for op in tech.get("opportunities", [])}

    scored = []
    for cand in discovered.get("candidates", []):
        code = str(cand["code"])
        base_score, rationale = heuristic_score(cand)
        extras = {}
        role = "discovery"

        ov = overflow_by_code.get(code)
        if ov:
            role = ov.get("role", role)
            sat = overflow.get("leader_saturation", 0) if role == "leader" else 0
            disc = ov.get("discount", 0)
            elas = ov.get("elasticity", 0)
            extras.update({"saturation": sat, "discount": disc, "elasticity": elas})
            if role == "second_tier":
                bonus = max(0, disc) * 30 + elas * 5
                base_score = min(100, base_score + int(bonus))
                rationale.append(f"二线折价 {disc*100:.0f}% 弹性 {elas} (+{int(bonus)})")
            elif role == "leader":
                rationale.append(f"龙头(饱和度 {sat})")

        top = tech_by_code.get(code)
        if top:
            role = "tech_option"
            ev = top.get("expected_value", 0)
            extras["tech_option_value"] = ev
            extras["tech"] = top.get("tech", "")
            extras["stage"] = top.get("stage", "")
            bonus = int(ev * 30)
            base_score = min(100, base_score + bonus)
            rationale.append(f"技术期权({top.get('tech','')}, EV {ev:.2f}, +{bonus})")

        # ===== a-stock-data skill 集成：4 维 enrich bonus =====
        ex = (enriched_extras or {}).get(code) or {}

        # 龙虎榜机构净买（万元）→ 每百万 +1，上限 +15
        dt = ex.get("dragon_tiger") or {}
        inst_net = (dt.get("institution") or {}).get("net_amt")
        if inst_net and inst_net > 0:
            extras["dragon_tiger_inst_net"] = inst_net
            bonus = min(15, int(inst_net / 100))
            base_score = min(100, base_score + bonus)
            rationale.append(f"龙虎榜机构净买 {inst_net:.0f}万 (+{bonus})")

        # 120 日主力净流入（元）→ 每亿 +1 上限 +10；净流出 -5
        ff = ex.get("fund_flow_120d") or {}
        inflow = ff.get("main_net_inflow")
        if inflow is not None and inflow != 0:
            extras["fund_flow_120d"] = inflow
            if inflow > 0:
                bonus = min(10, int(inflow / 1e8))
                base_score = min(100, base_score + bonus)
                rationale.append(f"120日主力净流入 {inflow/1e8:.1f}亿 (+{bonus})")
            else:
                base_score = max(0, base_score - 5)
                rationale.append(f"120日主力净流出 {inflow/1e8:.1f}亿 (-5)")

        # 融资余额增长 >10% → +3
        mg = ex.get("margin") or {}
        chg = mg.get("margin_balance_change")
        if chg and chg > 0.1:
            extras["margin_change_pct"] = chg
            base_score = min(100, base_score + 3)
            rationale.append(f"融资余额+{chg*100:.0f}% (+3)")

        # 研报评级 + 一致预期 EPS（不直接加分，作为 LLM 上下文）
        rpts = ex.get("research_reports") or []
        if rpts:
            top_rpt = rpts[0]
            extras["top_report_rating"] = top_rpt.get("rating", "")
            extras["consensus_eps"] = top_rpt.get("predict_this_year_eps")
            extras["top_report_org"] = top_rpt.get("org", "")

        # 解禁 warning 标记（已在 filter_by_lockup 设置，透传到 extras）
        if ex.get("lockup_warning"):
            extras["lockup_warning"] = True
            base_score = max(0, base_score - 5)
            rationale.append("近期解禁 (-5)")

        # THS 热点题材 tags（透传，不加分但供 LLM 引用）
        if ex.get("ths_topics"):
            extras["ths_topics"] = ex["ths_topics"]

        scored.append({
            "code": code,
            "name": cand["name"],
            "sector": cand["sector"],
            "tier": cand["tier"],
            "in_pool": cand["in_pool"],
            "news_hits": cand["news_hits"],
            "source": cand["source"],
            "score": base_score,
            "role": role,
            "rationale": rationale,
            "extras": extras,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    return {
        "sector": sector_under,
        "overflow_strength": overflow.get("overflow_strength", 0),
        "leader_saturation": overflow.get("leader_saturation", 0),
        "overflow_raw": overflow,
        "tech_raw": tech,
        "scored": scored,
    }


def render_scored_text(scored_result: Dict, top_n: int = 15) -> str:
    lines = [f"# 评分结果 - {scored_result['sector']}"]
    if scored_result.get("overflow_strength"):
        lines.append(
            f"龙头饱和度: {scored_result['leader_saturation']} | "
            f"溢出强度: {scored_result['overflow_strength']}/5"
        )
    lines.append("")
    lines.append(f"## Top {top_n} 候选")
    lines.append("| 排名 | 代码 | 名称 | 角色 | 分数 | 关键理由 |")
    lines.append("|------|------|------|------|------|---------|")
    for i, s in enumerate(scored_result["scored"][:top_n], 1):
        rationale = "; ".join(s["rationale"][:3])
        lines.append(
            f"| {i} | {s['code']} | {s['name']} | {s['role']} | {s['score']} | {rationale} |"
        )

    new_d = [s for s in scored_result["scored"] if s["source"] == "news"]
    if new_d:
        lines.append(f"\n## 🆕 动态新发现 ({len(new_d)} 只)")
        for s in new_d[:10]:
            lines.append(
                f"- **{s['code']} {s['name']}** (分数 {s['score']}) — "
                f"{'; '.join(s['rationale'][:2])}"
            )

    return "\n".join(lines)
