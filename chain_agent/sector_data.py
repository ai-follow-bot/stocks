"""共享「板块数据层」：板块关键词 + 核心公司 + 板块搜索 + 候选池 + 基础行情。

供 chain_agent / skills.deep-analyze / skills.valuation-lens 三路径统一调用，替代此前
三套各干各的采集（chain discover_candidates / deep 候选捞取 / val _candidates_from_discovery）。
只依赖 chain_agent 核心模块（不反向依赖 skill）。分析逻辑（打分/估值/decompose）仍留各路径。

gather(sector, days, top_n) -> {
  sector, canon, sector_name, keywords, core_companies,
  candidate_pool: [{code, name, source, segment_hint, sectors, mention_count}],
  board_evidence: [{id, source, title, snippet, url, publish_time}],  # T1/A1 统一编号
  data: {code: {name, pe, market_cap, change_pct}},
}

候选池来源（合并后 determine_sectors 多标签过滤，剔除非本板块）：
  core_companies 种子 + 板块搜索发现 + 财联社热度 + 档案召回。
"""

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from chain_agent import config


# ---------- 板块元信息 ----------
def _load_ecosystem() -> dict:
    try:
        return json.loads(config.ECOSYSTEM_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[sector_data] ecosystem 读取失败: {e}", file=sys.stderr)
        return {}


def canonical_sector_key(sector: str) -> str:
    """归一化到 sector_ecosystem.json 的 canonical key。

    顺序：原 key 直中 -> to_under -> 中文名反查（name==sector）-> to_under 兜底。
    """
    eco = _load_ecosystem()
    if sector in eco:
        return sector
    tu = config.to_under(sector)
    if tu in eco:
        return tu
    for k, v in eco.items():
        if k == "metadata" or not isinstance(v, dict):
            continue
        if v.get("name") == sector:
            return k
    return tu


def _sector_meta(canon: str) -> Tuple[str, List[str]]:
    """返回 (板块中文名, key_products)。key_products 空则回退 sector_keywords。"""
    eco = _load_ecosystem()
    sec = eco.get(canon) or {}
    name = sec.get("name") or canon
    kps = sec.get("key_products") or []
    if not kps:
        from chain_agent.discovery.stock_detector import SECTOR_KEYWORDS
        kps = SECTOR_KEYWORDS.get(canon) or []
    return name, kps


# ---------- 板块搜索（Tavily/Zhipu） -> board_evidence + 发现候选 ----------
def _board_search(sec_name: str, kps: List[str], max_results: int = 8) -> Tuple[List[dict], List[dict]]:
    """板块级搜索：返回 (evidence[T1..], detected_stocks)。

    evidence 每条 {id, source, title, snippet, url}，snippet 用共享 snippet() 锚定。
    """
    from chain_agent.collectors.orchestrator import _get_search_provider, _search_failed
    from chain_agent.collectors import search_cache
    from chain_agent.collectors.snippet import snippet as _snippet
    from chain_agent.collectors.zhipu_search import ZhipuSearch
    from chain_agent.discovery.stock_detector import StockDetector

    kw_tail = " ".join(kps[:3])
    queries = [
        f"{sec_name} 产业链 龙头 上市公司 A股 细分环节 {kw_tail} 2026".strip(),
        f"{sec_name} 玩家 市占率 卡脖子 国产替代 {kw_tail} 2026".strip(),
    ]
    provider, provider_name = _get_search_provider()
    zhipu = None  # 懒构造智谱（Tavily 失败时启用）
    evidence: List[dict] = []
    detected: List[dict] = []
    detector = StockDetector()
    t_idx = 0
    for q in queries:
        r = search_cache.get_cached(q)
        src = provider_name
        if not r and provider:
            try:
                r = provider.search_with_ai_summary(q, max_results=max_results)
            except Exception:
                r = None
            # Tavily 失败/无结果 -> 切智谱兜底
            if (r is None or _search_failed(r)) and src == "tavily" and config.ZHIPU_API_KEY:
                if zhipu is None:
                    try:
                        zhipu = ZhipuSearch()
                    except Exception:
                        zhipu = False
                if zhipu:
                    try:
                        r = zhipu.search_with_ai_summary(q, max_results=max_results)
                        src = "zhipu"
                    except Exception as e:
                        print(f"[sector_data] 智谱兜底失败 ({q[:30]}): {e}", file=sys.stderr)
        if not r or _search_failed(r):
            continue
        search_cache.set_cached(q, r)
        # answer 作为一个 evidence
        ans = r.get("answer") or ""
        if ans:
            t_idx += 1
            evidence.append({"id": f"T{t_idx}", "source": src or "web",
                             "title": f"{sec_name} 板块摘要", "snippet": _snippet(ans, 400),
                             "url": "", "publish_time": ""})
        for res in (r.get("results") or [])[:6]:
            t_idx += 1
            content = res.get("content") or ""
            evidence.append({
                "id": f"T{t_idx}", "source": src or "web",
                "title": res.get("title", ""),
                "snippet": _snippet(content, 500),
                "url": res.get("url", ""),
                "publish_time": res.get("publish_time") or res.get("published_date") or "",
            })
            for det in detector.detect_stocks_from_text(content[:1500]):
                detected.append(det)
    return evidence, detected


# ---------- 财联社热度 -> A* evidence + 候选 ----------
def _cailianshe_hot(sec_name: str, kps: List[str], days: int = 14) -> Tuple[List[dict], List[dict]]:
    """hermes latest_news.json + StockDetector。返回 (evidence[A1..], candidates)。

    candidates: [{code, name, source='cailianshe_hot', segment_hint, mention_count}]，按热度降序。
    """
    from chain_agent.discovery.stock_detector import StockDetector

    try:
        data = json.loads(config.HERMES_NEWS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    news = data.get("news") or []
    if not news:
        return [], []

    cutoff = datetime.now() - timedelta(days=days)
    kws = [sec_name] + [k for k in (kps or []) if k]
    detector = StockDetector()
    mention: Counter = Counter()
    names: Dict[str, str] = {}
    evidence: List[dict] = []
    a_idx = 0
    for n in news:
        pt = n.get("publish_time", "") or ""
        if pt:
            try:
                if datetime.fromisoformat(pt.replace("Z", "")) < cutoff:
                    continue
            except Exception:
                pass
        text = " ".join([str(n.get("title", "")), str(n.get("content", ""))[:800], str(n.get("brief", ""))[:400]])
        if not any(kw in text for kw in kws):
            continue
        a_idx += 1
        evidence.append({"id": f"A{a_idx}", "source": "cailianshe",
                         "title": n.get("title", ""), "snippet": text[:300],
                         "url": n.get("url", ""), "publish_time": pt})
        for det in detector.detect_stocks_from_text(text):
            code = det.get("code")
            if code:
                mention[code] += 1
                if not names.get(code):
                    names[code] = det.get("name", "")
    if not mention:
        return evidence, []
    top = mention.most_common(8)
    cands = [{"code": c, "name": names.get(c, ""), "source": "cailianshe_hot",
              "segment_hint": sec_name, "mention_count": cnt} for c, cnt in top]
    return evidence, cands


# ---------- 档案召回 ----------
def _recall_archive(canon: str) -> List[dict]:
    """从 valuation_stock_archive.json 召回该板块历史好标的（sectors_seen 含 canon）。"""
    from chain_agent.knowledge.archive import load_archive
    try:
        arc = load_archive()
    except Exception:
        return []
    out = []
    for code, rec in arc.items():
        if not isinstance(rec, dict):
            continue
        seen = rec.get("sectors_seen") or []
        if canon in seen:
            out.append({"code": code, "name": rec.get("name", ""), "source": "archive",
                        "segment_hint": rec.get("segment", "") or canon, "mention_count": 0})
    return out


# ---------- 主入口 ----------
def gather(sector: str, days: int = 14, top_n: int = 8) -> dict:
    """采集板块数据层：关键词 + 核心公司 + 候选池 + board_evidence + 基础行情。

    候选池合并后用 determine_sectors 多标签过滤：剔除非本板块（canon 不在其归属列表）的，
    保留 core_companies + 未归类 + 归属本板块的（跨板标的保留）。
    """
    from chain_agent.discovery.stock_detector import (
        StockDetector, load_core_companies, SECTOR_KEYWORDS,
    )
    from chain_agent.scoring.quotes import get_quote_provider

    canon = canonical_sector_key(sector)
    sec_name, kps = _sector_meta(canon)
    keywords = SECTOR_KEYWORDS.get(canon) or []
    core = load_core_companies(sector)
    print(f"[sector_data] === {sector} -> canon={canon} name={sec_name} | "
          f"keywords={len(keywords)} core={len(core)} key_products={len(kps)} ===", file=sys.stderr)

    detector = StockDetector()

    # 1. 板块搜索
    board_evi, board_detected = _board_search(sec_name, kps)
    # 2. 财联社热度
    cls_evi, cls_cands = _cailianshe_hot(sec_name, kps, days=days)
    # 3. 档案召回
    arc_cands = _recall_archive(canon)

    # 4. 合并候选池（去重，保 source 优先级）
    seen: Dict[str, dict] = {}
    for c in core:  # 核心公司种子优先
        code = c.get("code")
        if code and code not in seen:
            seen[code] = {"code": code, "name": c.get("name", ""), "source": "core_company",
                          "segment_hint": c.get("segment", "") or sec_name, "mention_count": 0}
    for det in board_detected:  # 板块搜索发现的
        code = det.get("code")
        if code and code not in seen:
            seen[code] = {"code": code, "name": det.get("name", ""), "source": "discovered",
                          "segment_hint": det.get("sector", "") or sec_name, "mention_count": 0}
    for c in cls_cands:  # 财联社热度
        if c["code"] not in seen:
            seen[c["code"]] = c
        else:
            seen[c["code"]]["mention_count"] = c.get("mention_count", 0)
    for c in arc_cands:  # 档案召回
        if c["code"] not in seen:
            seen[c["code"]] = c

    # 5. 相关性过滤：determine_sectors 多标签，剔除非本板块
    core_codes = {c.get("code") for c in core}
    filtered, dropped = [], []
    for c in seen.values():
        code = c.get("code")
        if code in core_codes:  # 核心公司无条件保留
            c["sectors"] = detector.determine_sectors(code)
            filtered.append(c); continue
        det_sectors = detector.determine_sectors(code) if code else []
        c["sectors"] = det_sectors
        if det_sectors and canon not in det_sectors:
            dropped.append(f"{c.get('name','')}({','.join(det_sectors[:2])})")
        else:
            filtered.append(c)
    if dropped:
        print(f"[sector_data] 相关性过滤剔除 {len(dropped)} 只其它板块股: {', '.join(dropped[:8])}",
              file=sys.stderr)

    # 6. 不截断：返回全量过滤后的候选池，由各路径按自己的策略截断
    pool = filtered

    # 7. 基础行情
    codes = [c["code"] for c in pool if c.get("code")]
    data: Dict[str, dict] = {}
    if codes:
        try:
            data = get_quote_provider().get_quotes(codes)
        except Exception as e:
            print(f"[sector_data] 行情拉取失败: {e}", file=sys.stderr)

    board_evidence = board_evi + cls_evi
    print(f"[sector_data] 候选池 {len(pool)} 只（剔除 {len(dropped)}）| "
          f"evidence {len(board_evidence)}（T{len(board_evi)}/A{len(cls_evi)}）| "
          f"行情 {sum(1 for v in data.values() if v)}/{len(codes)}", file=sys.stderr)

    return {
        "sector": sector,
        "canon": canon,
        "sector_name": sec_name,
        "keywords": keywords,
        "core_companies": core,
        "candidate_pool": pool,
        "board_evidence": board_evidence,
        "data": data,
    }
