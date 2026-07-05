"""
美股 Layer 4: 龙头溢出效应分析

基于 chain_agent/scoring/overflow.py 改写：
- 数据源从 sector_overflow_config.json → us_sector_overflow_config.json
- QuoteProvider 注入 FinnhubQuoteProvider
- 算法完全复用 A 股（市场无关的 PE/市值双维度评分）
"""

import json
from typing import Dict, List, Optional, Tuple

from us_chain_agent import config
from chain_agent.scoring.quotes import QuoteProvider


def _load_us_sector_config(sector_hyphen: str) -> Dict:
    """从 us_sector_overflow_config.json 取板块配置"""
    if not config.US_OVERFLOW_CONFIG_JSON.exists():
        return {}
    with open(config.US_OVERFLOW_CONFIG_JSON, "r", encoding="utf-8") as f:
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
    """分析美股板块溢出效应（接口对齐 A 股 overflow.analyze_overflow）"""
    sector_under = config.to_under(sector) if hasattr(config, "to_under") else sector.replace("-", "_")
    sector_hyphen = sector.replace("_", "-")
    sec_cfg = _load_us_sector_config(sector_hyphen)

    if not sec_cfg:
        return {
            "sector": sector_under,
            "leader_saturation": 0,
            "overflow_strength": 0,
            "leader_analysis": [],
            "candidates": [],
            "summary": f"美股板块 {sector_under} 未在 us_overflow_config 中配置",
        }

    # 龙头饱和度分析
    leaders = sec_cfg.get("leaders", [])
    leader_codes = [str(l["code"]) for l in leaders if l.get("code")]
    leader_analysis = []
    if leader_codes and quote_provider:
        quotes = quote_provider.get_quotes(leader_codes)
        pe_threshold = sec_cfg.get("leader_pe_threshold", 50)
        cap_threshold = sec_cfg.get("leader_cap_threshold", 500)
        for l in leaders:
            code = str(l["code"])
            q = quotes.get(code, {})
            pe = q.get("pe")
            cap = q.get("market_cap", 0) or 0
            sat, pe_s, cap_s = _calc_saturation(pe, cap, pe_threshold, cap_threshold)
            leader_analysis.append({
                "code": code,
                "name": l.get("name", ""),
                "pe": pe,
                "market_cap": cap,
                "saturation": sat,
                "pe_score": pe_s,
                "cap_score": cap_s,
            })
        leader_saturation = (
            sum(la["saturation"] for la in leader_analysis) / len(leader_analysis)
            if leader_analysis else 0
        )
    else:
        leader_saturation = 0

    # 二线弹性分析
    second_tier = sec_cfg.get("second_tier", [])
    candidates = []
    if second_tier and quote_provider:
        st_codes = [str(s["code"]) for s in second_tier if s.get("code")]
        quotes = quote_provider.get_quotes(st_codes)
        # 用龙头平均 PE 作为基准
        leader_avg_pe = (
            sum(la["pe"] for la in leader_analysis if la.get("pe")) / max(1, sum(1 for la in leader_analysis if la.get("pe")))
        ) if leader_analysis else None
        for s in second_tier:
            code = str(s["code"])
            q = quotes.get(code, {})
            pe = q.get("pe")
            cap = q.get("market_cap", 0) or 0
            discount = 0.0
            elasticity = 0
            if pe and leader_avg_pe and leader_avg_pe > 0:
                discount = max(0, (leader_avg_pe - pe) / leader_avg_pe)
                elasticity = min(5, int(discount * 5 + (1 if s.get("tech_option") else 0)))
            candidates.append({
                "code": code,
                "name": s.get("name", ""),
                "pe": pe,
                "market_cap": cap,
                "discount": discount,
                "elasticity": elasticity,
                "catalyst": s.get("catalyst", ""),
            })
        # 溢出强度 = 饱和度 × 0.5 + 平均弹性 × 0.5
        avg_elas = sum(c["elasticity"] for c in candidates) / len(candidates) if candidates else 0
        overflow_strength = leader_saturation * 0.5 + avg_elas * 0.5
    else:
        overflow_strength = leader_saturation * 0.5

    return {
        "sector": sector_under,
        "leader_saturation": leader_saturation,
        "overflow_strength": overflow_strength,
        "leader_analysis": leader_analysis,
        "candidates": candidates,
        "summary": f"龙头饱和度 {leader_saturation:.1f}, 溢出强度 {overflow_strength:.1f}",
    }
