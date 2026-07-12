"""
valuation-lens 主 pipeline（编排层）

以「稀缺 + 前瞻 + 供需」为第一性原理，对候选标的做估值排序。
当前 PE 仅作辅助确认，方向约束由 scoring._compute_valuation_score 确定性执行
（高 PE 不否决稀缺+前瞻强的标的；低 PE 不救三信号弱的标的）。

三种入口：
  analyze_chain(chain)   — 自动发现候选（板块搜索 + StockDetector + 财联社热度 + 档案召回）
  analyze_codes(codes)   — 显式代码列表
  analyze_stock(input)   — 单股深度估值判断

实现拆分：
  archive.py  — per-stock / 板块级知识档案（积累 + 增量更新 + 跨 skill 互通）
  search.py   — 候选发现 + 单标的 S/F/D 搜索（24h 复用档案 + 财联社实时）
  scoring.py  — PE 方向约束 + 估值分（确定性）+ LLM 三维打分（分批 + 逐只重试）
  analyzer.py — 本文件：行情注入 + pipeline 编排 + 三入口
"""

import json
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional

from chain_agent import config
from chain_agent.llm.client import get_llm_client
from chain_agent.llm.parse import json_from_llm
from chain_agent.knowledge.archive import strip_evidence_prefix as _strip_ev

from . import prompts
from .archive import (
    _get_sector_prior,
    _synthesize_sector_summary,
    _upsert_archive,
    _upsert_sector_archive,
)
from .search import (
    _canonical_sector_key,
    _candidates_from_discovery,
    _load_ecosystem,
    _load_stock_list,
    _name_of,
    _resolve_codes,
    search_all_candidates,
)
from .scoring import score_valuations


# ===== 工具 =====
def _llm_call(system: str, user: str) -> Optional[str]:
    client = get_llm_client()
    if client is None:
        print("[valuation-lens] LLM 不可用", file=sys.stderr)
        return None
    try:
        return client.synthesize(system, user)
    except Exception as e:
        print(f"[valuation-lens] LLM 调用失败: {e}", file=sys.stderr)
        return None


# ===== 主入口 =====
def _enrich_quotes(candidates: List[dict]) -> dict:
    """拉 PE/市值/涨跌幅 注入候选。返回 quotes dict。"""
    codes = [c["code"] for c in candidates if c.get("code")]
    if not codes:
        return {}
    try:
        from chain_agent.scoring.quotes import get_quote_provider
        quotes = get_quote_provider().get_quotes(codes) or {}
        for c in candidates:
            q = quotes.get(c["code"], {}) or {}
            c["pe"] = q.get("pe")
            c["market_cap"] = q.get("market_cap")
            c["change_pct"] = q.get("change_pct")
        print(f"[valuation-lens] 拉到 {len(quotes)} 只候选股的 PE/市值", file=sys.stderr)
        return quotes
    except Exception as e:
        print(f"[valuation-lens] 拉行情失败（PE/市值将为 null）: {e}", file=sys.stderr)
        return {}


def _run_chain_or_codes(chain_name: str, candidates: List[dict], days: int,
                        top_n: int, sector: Optional[str] = None,
                        quotes_data: Optional[dict] = None) -> dict:
    """chain / codes 共用的估值 pipeline。sector 非 None 时跑完 upsert 知识档案。

    quotes_data 非 None 时用共享板块数据层的行情（跳过 _enrich_quotes 重复拉）。
    """
    print(f"[valuation-lens] === {chain_name} | {len(candidates)} 只候选 "
          f"(days={days}, top_n={top_n}) ===", file=sys.stderr)

    if quotes_data:
        # 用共享板块数据层行情，不重复拉
        for c in candidates:
            q = quotes_data.get(c.get("code"), {}) or {}
            c["pe"] = q.get("pe")
            c["market_cap"] = q.get("market_cap")
            c["change_pct"] = q.get("change_pct")
    else:
        _enrich_quotes(candidates)

    # 截断到 top_n*2：档案召回 + 财联社热门优先占约 2/3 槽位，其余按市值
    cap = max(top_n, min(len(candidates), top_n * 2))
    def _is_priority(c):
        return c.get("source") == "archive" or (c.get("mention_count") or 0) >= 2
    priority = sorted([c for c in candidates if _is_priority(c)],
                      key=lambda c: (c.get("source") == "archive", c.get("mention_count") or 0),
                      reverse=True)
    others = sorted([c for c in candidates if not _is_priority(c)],
                    key=lambda c: -(c.get("market_cap") or 0))
    prio_keep = min(len(priority), cap * 2 // 3)
    candidates = priority[:prio_keep] + others[:max(0, cap - prio_keep)]
    src_counts = {}
    for c in candidates:
        src_counts[c.get("source", "?")] = src_counts.get(c.get("source", "?"), 0) + 1
    print(f"[valuation-lens] 候选截断: {len(candidates)} 只 {src_counts}", file=sys.stderr)

    # codes 模式（sector=None）不读档案（显式探索）；chain 模式读档案复用
    evidence_map = search_all_candidates(candidates, days=days,
                                         use_archive=sector is not None)
    all_failed = all(not (v.get("evidence") or {}) for v in evidence_map.values())

    sector_prior = _get_sector_prior(sector) if sector else None
    scoring = score_valuations(chain_name, candidates, evidence_map, sector_prior=sector_prior)
    llm_failed = scoring.get("llm_failed_count") or 0
    data_quality = "degraded" if (all_failed or llm_failed) else "ok"
    if sector:
        _upsert_archive(sector, (scoring.get("candidates") or []), evidence_map)
        sda = scoring.get("supply_demand_analysis") or ""
        if not sda:
            sda = _synthesize_sector_summary(scoring.get("candidates") or [])
            scoring["supply_demand_analysis"] = sda  # 兜底写回，让 report 供需概览段有内容
        _upsert_sector_archive(sector, sda)

    return {
        "mode": "chain",
        "chain_name": chain_name,
        "run_time": datetime.now().isoformat(),
        "days": days,
        "data_quality": data_quality,
        "candidates_in": [c["code"] for c in candidates],
        "scoring": scoring,
        "search_stats": {
            c["code"]: {"evidence_count": len((evidence_map.get(c["code"], {}) or {}).get("evidence", {})),
                        "provider": (evidence_map.get(c["code"], {}) or {}).get("provider")}
            for c in candidates
        },
    }


def analyze_chain(chain: str, days: int = 14, top_n: int = 8) -> dict:
    # 走共享板块数据层：统一采集候选池 + 板块 evidence + 基础行情
    from chain_agent import sector_data
    sd = sector_data.gather(chain, days=days, top_n=top_n)
    candidates = sd.get("candidate_pool") or []
    if not candidates:
        return {"error": f"板块 {chain} 自动发现候选失败（搜索无果或板块名无法识别）", "chain": chain}
    chain_name = sd.get("sector_name") or chain
    return _run_chain_or_codes(chain_name, candidates, days, top_n,
                               sector=sd.get("canon") or _canonical_sector_key(chain),
                               quotes_data=sd.get("data"))


def analyze_codes(codes: List[str], days: int = 14, top_n: int = 8) -> dict:
    candidates = _resolve_codes(codes)
    if not candidates:
        return {"error": "未提供有效代码"}
    return _run_chain_or_codes("explicit-codes", candidates, days, top_n)


def analyze_stock(stock_input: str, days: int = 14) -> dict:
    """单股估值判断。"""
    print(f"[valuation-lens] === stock 模式: {stock_input} ===", file=sys.stderr)

    # 1. 定位公司
    if re.match(r"^\d{6}$", stock_input):
        stock_code = stock_input
        stock_name = _name_of(stock_code)
        if not stock_name:
            return {"error": f"未在 A 股名单中找到 {stock_input}"}
    else:
        # 公司名 → 反查代码
        sl = _load_stock_list()
        hit = None
        for code, info in sl.items():
            nm = info.get("name", "") if isinstance(info, dict) else str(info)
            if nm == stock_input:
                hit = (code, nm)
                break
            if nm and stock_input in nm:
                hit = (code, nm)
        if not hit:
            return {"error": f"无法识别股票: {stock_input}"}
        stock_code, stock_name = hit

    print(f"[valuation-lens] 定位: {stock_name}（{stock_code}）", file=sys.stderr)

    # 2. LLM 识别主营 + 产业链 + 环节（给 ecosystem 板块列表，让 LLM 返回标准板块名以便归一化到 canonical key）
    sector_names = [v.get("name") for k, v in _load_ecosystem().items()
                    if k != "metadata" and isinstance(v, dict) and v.get("name")]
    identify = f"""请用一行 JSON 回答（不要代码块、不要解释）：
公司「{stock_name}（{stock_code}）」的主营业务、所处具体环节，以及所属产业链。
所属产业链从以下板块中选**最匹配的一个**；仅当某板块与公司主营高度匹配时才选它，**若以下板块均不贴切，chain_name 给空字符串""（不要勉强选择）**。
可选板块: {"、".join(sector_names)}
格式：{{"business":"...","chain_name":"<匹配的板块名或空字符串>","segment":"..."}}"""
    text = _llm_call("你是一位 A 股产业研究员，回答要简短准确。", identify)
    company_info = json_from_llm(text) if text else {}
    if not company_info:
        return {"error": f"无法定位 {stock_name} 的产业链", "raw_llm": text}

    # 3. 单股估值打分（复用 score_valuations，注板块 prior + upsert 档案，与 chain 模式一致）
    chain_name = company_info.get("chain_name", "")
    candidates = [{"code": stock_code, "name": stock_name, "source": "explicit",
                   "segment_hint": company_info.get("segment", "")}]
    _enrich_quotes(candidates)
    evidence_map = search_all_candidates(candidates, days=days, use_archive=True)
    canon = _canonical_sector_key(chain_name) if chain_name else ""
    sector_prior = _get_sector_prior(canon) if canon else None
    scoring = score_valuations(chain_name, candidates, evidence_map, sector_prior=sector_prior)
    single = (scoring.get("candidates") or [{}])[0]
    # 始终写 per-stock 档案：canon 匹配 ecosystem 用标准 key，否则标 "unclassified"
    # （ecosystem 外的股仍积累 evidence/prev_score 享 24h 复用 + 走势；unclassified 不在 ecosystem，不进板块召回）
    _upsert_archive(canon or "unclassified", scoring.get("candidates") or [], evidence_map)

    # 4. LLM 估值判断（叙述）
    q = candidates[0]
    kf = (evidence_map.get(stock_code, {}) or {}).get("key_facts") or {}
    if kf.get("S") or kf.get("F") or kf.get("D"):
        prior = ("\n# 历史认知 prior（档案上次综合，须用本次 evidence 增量更新，勿照搬）\n"
                 f"- 稀缺: {_strip_ev(kf.get('S',''))}\n"
                 f"- 前瞻: {_strip_ev(kf.get('F',''))}\n"
                 f"- 供需: {_strip_ev(kf.get('D',''))}\n")
    else:
        prior = ""
    user = prompts.STOCK_VERDICT_USER_TEMPLATE.format(
        stock_name=stock_name, stock_code=stock_code,
        business=company_info.get("business", ""),
        chain_name=company_info.get("chain_name", ""),
        segment=company_info.get("segment", ""),
        pe=q.get("pe"), market_cap=q.get("market_cap"), change_pct=q.get("change_pct"),
        prior=prior,
        evidence_text=(evidence_map.get(stock_code, {}) or {}).get("content_text", "")[:3000],
        scoring_text=json.dumps(single, ensure_ascii=False, indent=2)[:2000],
    )
    verdict_md = _llm_call(prompts.STOCK_VERDICT_SYSTEM, user) or "(LLM 不可用，无法生成判断)"

    llm_failed = scoring.get("llm_failed_count") or 0
    all_failed = not (evidence_map.get(stock_code, {}) or {}).get("evidence")
    return {
        "mode": "stock",
        "stock_code": stock_code,
        "stock_name": stock_name,
        "company_info": company_info,
        "scoring": single,
        "verdict_md": verdict_md,
        "data_quality": "degraded" if (all_failed or llm_failed) else "ok",
        "run_time": datetime.now().isoformat(),
    }
