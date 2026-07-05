"""
Layer 4: 启发式兜底评分

无行情数据 / 板块未在 overflow_config 配置时使用。
基于 news_hits + importance + in_pool + tier + 代码可投性。
"""

import math
from typing import Dict, Tuple, List


def heuristic_score(cand: Dict) -> Tuple[int, List[str]]:
    """
    返回 (score 0-100, rationale_list)
    """
    score = 0
    rationale = []

    # 新闻命中 (max 25 命中次数 + 15 重要性加权 = 40)
    # P1-1/P2-2: 对数缩放避免 5 次即溢出封顶；引入财联 importance 字段
    nh = cand.get("news_hits", 0)
    importance_sum = cand.get("news_importance_sum", 0) or 0
    base_news = min(25, int(math.log(nh + 1) * 12))   # nh=1→12, 3→24, 5→25(cap), 10→25(cap)
    importance_bonus = min(15, int(importance_sum * 3))
    news_score = base_news + importance_bonus
    score += news_score
    if nh:
        rationale.append(
            f"新闻命中 {nh} 次 (+{base_news}, 重要性 +{importance_bonus})"
        )

    # 池中已有 (+15)
    if cand.get("in_pool"):
        score += 15
        rationale.append("已在配置池 (+15)")

    # A 股可投 (+20)
    code = str(cand.get("code", ""))
    if code.isdigit() and len(code) == 6:
        score += 20
        rationale.append("A股可投 (+20)")
    else:
        rationale.append(f"非A股代码({code}) (+0)")

    # 来源多样性 (+10)
    if cand.get("source") == "both":
        score += 10
        rationale.append("新闻+池双确认 (+10)")

    # Tier 核心 (+15) / 下游 (+5)
    if cand.get("tier", 2) <= 2:
        score += 15
        rationale.append(f"Tier{cand['tier']} 核心环节 (+15)")
    else:
        score += 5
        rationale.append(f"Tier{cand['tier']} 下游 (+5)")

    return min(100, score), rationale
