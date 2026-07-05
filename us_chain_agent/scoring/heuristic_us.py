"""
美股 Layer 4: 启发式兜底评分

基于 chain_agent/scoring/heuristic.py 改写：
- "A 股可投 (6 位数字代码)" → "美股可投 (2-5 位字母代码 + 在 us_stock_list)"
"""

import math
import re
from typing import Dict, List, Tuple

# 美股 ticker 正则：2-5 位大写字母
_US_TICKER_RE = re.compile(r"^[A-Z]{2,5}$")


def heuristic_score(cand: Dict) -> Tuple[int, List[str]]:
    """返回 (score 0-100, rationale_list)"""
    score = 0
    rationale = []

    # 新闻命中 (max 25 命中次数 + 15 重要性加权 = 40)
    nh = cand.get("news_hits", 0)
    importance_sum = cand.get("news_importance_sum", 0) or 0
    base_news = min(25, int(math.log(nh + 1) * 12))
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

    # 美股可投 (+20)
    code = str(cand.get("code", ""))
    if _US_TICKER_RE.match(code):
        score += 20
        rationale.append("美股可投 (+20)")
    else:
        rationale.append(f"非美股代码({code}) (+0)")

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
