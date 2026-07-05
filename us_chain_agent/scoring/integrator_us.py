"""
美股 Layer 4: 评分整合

基于 chain_agent/scoring/integrator.py 改写：
- heuristic → heuristic_us（美股可投判定）
- overflow → overflow_us（读 us_sector_overflow_config）
- tech_option → 复用 A 股（市场无关，Tavily 搜索驱动）
- quote_provider → 默认用 FinnhubQuoteProvider
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from chain_agent import config
from chain_agent.scoring.tech_option import analyze_tech_options

from us_chain_agent.scoring.heuristic_us import heuristic_score
from us_chain_agent.scoring.overflow_us import analyze_overflow
from us_chain_agent.scoring.quotes_us import FinnhubQuoteProvider, get_quote_provider


def score_candidates(sector: str, discovered: Dict,
                     quote_provider=None,
                     tavily_search=None,
                     tavily_results_pool: List[Dict] = None) -> Dict:
    """
    对美股候选标的打分。

    Returns: 与 A 股 score_candidates 同结构
    """
    sector_under = config.to_under(sector)

    if quote_provider is None:
        quote_provider = get_quote_provider()

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_ov = ex.submit(analyze_overflow, sector, quote_provider)
        fut_tech = ex.submit(
            analyze_tech_options, sector, tavily_search, tavily_results_pool
        )
        overflow = fut_ov.result()
        tech = fut_tech.result()

    overflow_by_code = {}
    if not overflow.get("summary", "").startswith("板块") and not overflow.get("summary", "").startswith("美股板块"):
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
    """复用 A 股 render_scored_text 算法（市场无关）"""
    # 直接 import A 股版本
    from chain_agent.scoring.integrator import render_scored_text as _render
    return _render(scored_result, top_n=top_n)
