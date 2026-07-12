"""cycle-lens 编排（SPEC §8，对齐 sector-strategy）：

板块模式调 sector_data.gather() 共享数据层 -> 候选池逐只跑周期分析。
单股模式直采单只数据（不走 gather）。
每只：data 采集 -> decompose 分解 -> archive 上修下修 -> LLM 三问+8步 -> 结果。
"""

import json
import sys
from datetime import datetime
from typing import Optional

from chain_agent import config
from . import data as data_mod
from . import decompose
from . import archive
from . import prompts


def _resolve_stock(stock_input: str):
    """股票名/代码 -> (code, name)。复用 a_stock_list 反查。"""
    s = stock_input.strip()
    if s.isdigit() and len(s) == 6:
        # 代码 -> 名
        try:
            sl = json.loads(config.STOCK_LIST_JSON.read_text(encoding="utf-8"))
            stocks = sl.get("stocks", sl)
            info = stocks.get(s)
            name = info.get("name", s) if isinstance(info, dict) else s
            return s, name
        except Exception:
            return s, s
    # 名 -> 代码
    try:
        sl = json.loads(config.STOCK_LIST_JSON.read_text(encoding="utf-8"))
        stocks = sl.get("stocks", sl)
        for code, info in stocks.items():
            if isinstance(info, dict) and info.get("name") == s:
                return code, s
    except Exception:
        pass
    return None, None


def _llm_judgment(code: str, name: str, decomp: dict, revision: dict,
                  data: dict, sector_keywords: str = "") -> dict:
    """调 LLM 三问 + 8步闭环。失败返回空 dict（降级）。"""
    from chain_agent.llm.client import get_llm_client
    from chain_agent.llm.parse import json_from_llm
    client = get_llm_client()
    if client is None:
        print("[cycle-lens] LLM 不可用", file=sys.stderr)
        return {}
    user = prompts.build_user(name, code, decomp, revision, data, sector_keywords)
    try:
        text = client.synthesize(prompts.CYCLE_SYSTEM, user)
        parsed = json_from_llm(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception as e:
        print(f"[cycle-lens] LLM 调用/解析失败 {code}: {e}", file=sys.stderr)
    return {}


def run_one(code: str, name: str, days: int = 14, sector_keywords: str = "") -> dict:
    """单只周期分析：data -> decompose -> archive -> LLM。"""
    print(f"[cycle-lens] --- {code} {name} ---", file=sys.stderr)
    data = data_mod.collect(code, days)
    decomp = decompose.decompose(
        data["price_hist"], data["eps_hist"],
        current_pe=data.get("current_pe"),
        predict_eps=(data.get("predict_eps") or {}).get("this"),
    )
    pe = (data.get("predict_eps") or {})
    revision = archive.upsert_predict_eps(code, name, pe.get("this"), pe.get("next"))
    llm = _llm_judgment(code, name, decomp, revision, data, sector_keywords)

    # data_quality：关键数据缺失则降级
    miss = []
    if not data["price_hist"]:
        miss.append("price_hist")
    if not data["eps_hist"]:
        miss.append("eps_hist")
    if decomp.get("classification") == "数据不足":
        miss.append("decompose")
    data_quality = "degraded" if miss else "ok"

    return {
        "code": code, "name": name,
        "decomp": decomp, "revision": revision, "llm_judgment": llm,
        "data_quality": data_quality, "missing": miss,
        "predict_eps": data.get("predict_eps"),
        "current_pe": data.get("current_pe"),
        "market_cap": data.get("market_cap"),
    }


def run_cycle_lens(stock: Optional[str] = None, sector: Optional[str] = None,
                   days: int = 14, top_n: int = 15) -> dict:
    """主入口。--stock 单股直采；--chain 走 sector_data.gather 共享候选池。"""
    print(f"[cycle-lens] #### 业绩-估值周期镜头 | stock={stock or '(无)'} "
          f"sector={sector or '(无)'} days={days} ####", file=sys.stderr)

    if sector:
        from chain_agent import sector_data
        sd = sector_data.gather(sector, days=days, top_n=top_n)
        pool = sd.get("candidate_pool") or []
        keywords = ", ".join(sd.get("keywords") or []) or None
        sector_name = sd.get("sector_name") or sector
        print(f"[cycle-lens] 板块 {sector_name} 候选池 {len(pool)} 只，逐只跑周期分析", file=sys.stderr)
        results = []
        for c in pool[:top_n]:  # 截断到 top_n
            try:
                results.append(run_one(c["code"], c.get("name", ""), days, sector_keywords=keywords or ""))
            except Exception as e:
                print(f"[cycle-lens] {c.get('code')} 失败: {e}", file=sys.stderr)
                results.append({"code": c.get("code"), "name": c.get("name", ""),
                                "error": str(e), "data_quality": "degraded"})
        summary = _summarize_chain(results, sector_name)
        return {
            "mode": "chain", "sector": sector, "sector_name": sector_name,
            "keywords": sd.get("keywords") or [],
            "results": results, "summary": summary,
            "run_time": datetime.now().isoformat(),
        }

    # 单股
    code, name = _resolve_stock(stock)
    if not code:
        return {"error": f"无法识别股票：{stock}", "stock_input": stock}
    one = run_one(code, name, days)
    return {
        "mode": "stock", "stock": stock, "code": code, "name": name,
        "result": one, "run_time": datetime.now().isoformat(),
    }


def _summarize_chain(results: list, sector_name: str) -> dict:
    """板块汇总：各分类分布 + 警惕信号统计。"""
    from collections import Counter
    classes = Counter(r.get("decomp", {}).get("classification", "N/A") for r in results if not r.get("error"))
    warnings = [r["name"] for r in results
                if r.get("llm_judgment", {}).get("warning", "").startswith(("已出现", "观察中"))]
    return {
        "sector": sector_name,
        "count": len(results),
        "classification_dist": dict(classes),
        "warning_stocks": warnings[:10],
    }
