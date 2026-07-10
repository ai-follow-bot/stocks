"""ce-value 编排：431 中国特色价值投资。

宏观 -> 市场 -> 行业(选板块) -> 公司(harness 三视角) -> 三高筛选 -> 卡脖子抓手。
公司层复用 skills.harness.orchestrator.run_harness_chain（in-process，不再包一层 subprocess）。
多板块串行跑 harness（避免 N×3 子进程并发把 kimi 配额打满）。
"""

import sys
from datetime import datetime
from typing import List, Optional

from skills.harness import orchestrator as harness_orch

from . import macro, market, sector_picker, financials, three_high


def _run_company_layer(sector: str, days: int, top_n: int) -> dict:
    """单板块公司层：harness 三视角 + 三高财务打分。"""
    print(f"[ce-value] === 公司层: {sector}（harness 三视角）===", file=sys.stderr)
    h = harness_orch.run_harness_chain(sector, days=days, top_n=top_n)
    aligned = h.get("aligned", []) or []
    codes = [a.get("code") for a in aligned if a.get("code")]
    fins = financials.get_financials_batch(codes) if codes else {}
    three_high.score_batch(aligned, fins)
    return {
        "sector": sector,
        "aligned": aligned,
        "deep_bottlenecks": h.get("deep_bottlenecks", {"top_bottlenecks": [], "segments": []}),
        "paths": h.get("paths", {}),
        "path_errors": h.get("path_errors", {}),
        "financials_hit": f"{sum(1 for v in fins.values() if v)}/{len(codes)}",
        "harness_synthesis": h.get("synthesis", ""),
    }


def run_ce_value(sector: Optional[str] = None, days: int = 14, top_n: int = 8,
                 max_sectors: int = 2) -> dict:
    """431 主入口。

    sector 给定 -> 用户指定模式（1 个板块）；None -> LLM 从宏观/市场选 1-max_sectors 板块。
    返回 431 结构 dict（供 report.render_report 渲染）。
    """
    print(f"[ce-value] #### 431 中国特色价值投资 | sector={sector or '(自动选)'} "
          f"days={days} top_n={top_n} max_sectors={max_sectors} ####", file=sys.stderr)

    # 1. 宏观层
    macro_brief = macro.run_macro_briefing()
    # 2. 市场层
    market_brief = market.run_market_briefing()

    # 3. 行业层：选定板块
    if sector:
        sectors: List[str] = [sector]
        pick_reason = "用户指定"
    else:
        pick = sector_picker.pick_sectors(macro_brief, market_brief, max_pick=max_sectors)
        sectors = pick["picked"]
        pick_reason = pick.get("reason", "")

    # 4+5+6. 公司层 + 三高 + 卡脖子（每板块串行）
    company_results = []
    for s in sectors:
        try:
            company_results.append(_run_company_layer(s, days, top_n))
        except Exception as e:
            print(f"[ce-value] 板块 {s} 公司层失败: {e}", file=sys.stderr)
            company_results.append({
                "sector": s, "aligned": [], "deep_bottlenecks": {"top_bottlenecks": [], "segments": []},
                "paths": {}, "path_errors": {"_ce_value": str(e)}, "financials_hit": "0/0",
                "harness_synthesis": "",
            })

    return {
        "mode": "chain" if sector else "auto",
        "sectors": sectors,
        "macro": macro_brief,
        "market": market_brief,
        "pick_reason": pick_reason,
        "company_results": company_results,
        "days": days,
        "top_n": top_n,
        "run_time": datetime.now().isoformat(),
    }
