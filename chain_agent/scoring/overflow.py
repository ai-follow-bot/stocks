"""
Layer 4: 龙头溢出效应分析（重写）

算法借鉴 ~/.hermes/scripts/investment-research/active_opportunity_integrator.py 的
SectorOverflowAnalyzer（独立重写）。

输入板块，输出：
- 龙头饱和度（1-5）：PE + 市值双维度评分
- 二线弹性（1-5）：相对龙头 PE 折价 + 估值弹性
- 溢出强度（0-5）：饱和度 × 0.5 + 弹性 × 0.5
"""

import json
from typing import Dict, List, Optional, Tuple

from .. import config
from .quotes import QuoteProvider, get_quote_provider


def _load_sector_config(sector_hyphen: str) -> Dict:
    """从 sector_overflow_config.json 取板块配置"""
    if not config.OVERFLOW_CONFIG_JSON.exists():
        return {}
    with open(config.OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
        all_cfg = json.load(f)
    return all_cfg.get(sector_hyphen, {})


def _calc_saturation(pe: Optional[float], cap: float,
                     pe_threshold: float, cap_threshold: float) -> Tuple[float, int, int]:
    """计算饱和度：返回 (saturation_score, pe_score, cap_score)"""
    pe_score = 2
    if pe:
        if pe > pe_threshold * 1.5:
            pe_score = 5
        elif pe > pe_threshold:
            pe_score = 4
        elif pe > pe_threshold * 0.7:
            pe_score = 3
        elif pe > pe_threshold * 0.5:
            pe_score = 2
        else:
            pe_score = 1

    cap_score = 1
    if cap > cap_threshold * 3:
        cap_score = 5
    elif cap > cap_threshold * 2:
        cap_score = 4
    elif cap > cap_threshold:
        cap_score = 3
    elif cap > cap_threshold * 0.5:
        cap_score = 2
    else:
        cap_score = 1

    return (pe_score + cap_score) / 2, pe_score, cap_score


def analyze_overflow(sector: str, quote_provider: QuoteProvider = None) -> Dict:
    """
    分析板块溢出效应。

    Returns:
        {sector, leader_saturation, overflow_strength, leader_analysis, candidates, summary}
    """
    sector_under = config.to_under(sector)
    sector_hyphen = config.to_hyphen(sector)
    sec_cfg = _load_sector_config(sector_hyphen)

    if not sec_cfg:
        return {
            "sector": sector_under,
            "leader_saturation": 0,
            "overflow_strength": 0,
            "leader_analysis": [],
            "candidates": [],
            "summary": f"板块 {sector_under} 未在 overflow_config 中配置",
        }

    if quote_provider is None:
        quote_provider = get_quote_provider()

    leaders_cfg = sec_cfg.get("leaders", [])
    second_tier_cfg = sec_cfg.get("second_tier", [])
    pe_threshold = sec_cfg.get("leader_pe_threshold", 40)
    cap_threshold = sec_cfg.get("leader_cap_threshold", 300)

    # 拉行情
    all_codes = [s["code"] for s in leaders_cfg + second_tier_cfg]
    quotes = quote_provider.get_quotes(all_codes)

    # enrich leaders
    leader_analysis = []
    for s in leaders_cfg:
        code = s["code"]
        q = quotes.get(code, {})
        pe = q.get("pe")
        cap = q.get("market_cap", 0)
        sat, pe_s, cap_s = _calc_saturation(pe, cap or 0, pe_threshold, cap_threshold)
        leader_analysis.append({
            "code": code,
            "name": q.get("name", s.get("name", "")),
            "symbol": s.get("symbol", f"{code}.SZ"),
            "pe": pe,
            "market_cap": cap,
            "change_pct": q.get("change_pct", 0),
            "saturation": sat,
            "pe_score": pe_s,
            "cap_score": cap_s,
        })

    avg_saturation = (
        sum(l["saturation"] for l in leader_analysis) / len(leader_analysis)
        if leader_analysis else 0
    )

    # 二线弹性
    leader_pes = [l["pe"] for l in leader_analysis if l["pe"]]
    leader_avg_pe = sum(leader_pes) / max(1, len(leader_pes))

    candidates = []
    for s in second_tier_cfg:
        code = s["code"]
        q = quotes.get(code, {})
        pe = q.get("pe")
        cap = q.get("market_cap", 0)

        discount = 0
        if pe is None or pe < 0:
            discount = -1.0  # 亏损/无 PE
        elif leader_avg_pe and leader_avg_pe > 0:
            discount = 1 - (pe / leader_avg_pe)

        if pe is None or pe < 0:
            elasticity = 2
        elif discount > 0.4:
            elasticity = 5
        elif discount > 0.3:
            elasticity = 4
        elif discount > 0.2:
            elasticity = 3
        elif discount > 0.1:
            elasticity = 2
        else:
            elasticity = 1

        candidates.append({
            "code": code,
            "name": q.get("name", s.get("name", "")),
            "symbol": s.get("symbol", f"{code}.SZ"),
            "pe": pe,
            "market_cap": cap,
            "change_pct": q.get("change_pct", 0),
            "discount": discount,
            "elasticity": elasticity,
            "catalyst": s.get("catalyst", ""),
            "tech_option": s.get("tech_option", False),
            "tech": s.get("tech", ""),
            "stage": s.get("stage", ""),
        })

    avg_elasticity = (
        sum(c["elasticity"] for c in candidates) / max(1, len(candidates))
        if candidates else 0
    )
    overflow_strength = min(5.0, avg_saturation * 0.5 + avg_elasticity * 0.5)

    summary = (
        f"龙头饱和度 {avg_saturation:.1f}/5, "
        f"二线平均弹性 {avg_elasticity:.1f}/5, "
        f"溢出强度 {overflow_strength:.1f}/5"
    )

    return {
        "sector": sec_cfg.get("sector_name", sector_under),
        "leader_saturation": round(avg_saturation, 1),
        "overflow_strength": round(overflow_strength, 1),
        "leader_analysis": leader_analysis,
        "candidates": candidates,
        "summary": summary,
    }
