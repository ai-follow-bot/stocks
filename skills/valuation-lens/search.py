"""valuation-lens 候选发现 + 单标的搜索。

候选发现（自动，不依赖 overflow_config 手填）：板块搜索 + StockDetector +
财联社热度 + 档案召回。单标的搜索 S/F/D evidence：24h 内复用档案跳过 Tavily，
财联社始终实时拉（[新增] 标记自上次以来新发）。
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from chain_agent import config
from chain_agent.collectors import search_cache
from chain_agent.collectors.orchestrator import _get_search_provider
from chain_agent.collectors.snippet import snippet as _snippet
from chain_agent.knowledge.archive import load_archive as _load_archive

from .archive import (
    _archive_is_fresh,
    _cailianshe_per_stock,
    _recall_archive_candidates,
)


# ===== 股票名单辅助 =====
def _load_stock_list() -> dict:
    """加载 a_stock_list.json，返回 code→{name} 映射（兼容两种 schema）。"""
    try:
        d = json.loads(config.STOCK_LIST_JSON.read_text(encoding="utf-8"))
        stocks = d.get("stocks", d)
        if isinstance(stocks, dict):
            return {k: (v if isinstance(v, dict) else {"name": str(v)}) for k, v in stocks.items()}
        return {}
    except Exception:
        return {}


def _name_of(code: str) -> str:
    info = _load_stock_list().get(code)
    if not info:
        return ""
    return info.get("name", "") if isinstance(info, dict) else str(info)


def _resolve_codes(codes: List[str]) -> List[dict]:
    """显式代码列表 → 候选（name 反查 a_stock_list）。"""
    sl = _load_stock_list()
    out = []
    seen = set()
    for c in codes:
        c = str(c).strip().zfill(6) if str(c).strip().isdigit() else str(c).strip()
        if c in seen:
            continue
        seen.add(c)
        info = sl.get(c)
        name = info.get("name", "") if isinstance(info, dict) else (str(info) if info else "")
        out.append({"code": c, "name": name, "source": "explicit", "segment_hint": ""})
    return out


# ===== 1. 候选发现（自动，不依赖 overflow_config 手填配置）=====
def _load_ecosystem() -> dict:
    """加载 sector_ecosystem.json（取板块中文名 + key_products 用于搜索 query）。"""
    try:
        return json.loads(config.ECOSYSTEM_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_key_map() -> dict:
    """加载 data/sector_key_map.json（中文/大写别名 → canonical 英文 key）。"""
    try:
        return json.loads((config.ECOSYSTEM_JSON.parent / "sector_key_map.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _canonical_sector_key(sector: str) -> str:
    """归一化到 sector_ecosystem.json 的 canonical 英文 key。
    优先级：中英对照表(data/sector_key_map.json) → 英文 key 精确匹配 → name 反查(优先英文 key) → 中文 key 兜底 → to_under 回退。
    对照表用于把已删除的中文 duplicate（如 '光模块'）或别名映射到 canonical 英文 key。"""
    eco = _load_ecosystem()
    km = _load_key_map()
    # 0. 对照表映射（仅当映射结果在 ecosystem 时采用）
    mapped = km.get(sector)
    if mapped and mapped in eco:
        return mapped
    # 1. 英文 key 精确匹配（含 to_under 归一化）
    if sector in eco and sector.isascii():
        return sector
    tu = config.to_under(sector)
    if tu in eco and tu.isascii():
        return tu
    # 2. 中文名 name 反查 → 优先英文 key
    name_matches = [k for k, v in eco.items()
                    if k != "metadata" and isinstance(v, dict) and v.get("name") == sector]
    if name_matches:
        ascii_keys = [k for k in name_matches if k.isascii()]
        if ascii_keys:
            return ascii_keys[0]
        return name_matches[0]
    # 3. 中文 key 兜底
    if sector in eco:
        return sector
    if tu in eco:
        return tu
    return tu


def _candidates_from_discovery(sector: str, days: int = 14) -> List[dict]:
    """自动发现候选：板块级搜索 + StockDetector 识别 A 股标的。

    不读 sector_overflow_config.json（无手填候选/角色/forward_hint）。
    返回 [{code, name, source='discovered', segment_hint=<板块中文名>}]。
    """
    from chain_agent.discovery.stock_detector import StockDetector, SECTOR_KEYWORDS

    eco = _load_ecosystem()
    canon = _canonical_sector_key(sector)  # 归一化到 ecosystem canonical key
    sec_cfg = eco.get(canon) or {}
    sec_name = sec_cfg.get("name") or sector  # 中文名用于搜索
    # key_products 优先；空则回退 sector_keywords（让空壳板块也能用关键词收窄搜索/财联社过滤）
    kps = sec_cfg.get("key_products") or SECTOR_KEYWORDS.get(canon) or []
    kw_tail = " ".join(kps[:3])  # 环节产品词拼进 query 提升召回

    provider, provider_name = _get_search_provider()
    queries = [
        f"{sec_name} 产业链 龙头 上市公司 A股 细分环节 {kw_tail} 2026".strip(),
        f"{sec_name} 玩家 市占率 卡脖子 国产替代 {kw_tail} 2026".strip(),
    ]
    detector = StockDetector()
    seen: Dict[str, dict] = {}
    # 板块核心公司（类目）作种子候选，优先纳入（支持动态补充，方便分析时锚定龙头）
    from chain_agent.discovery.stock_detector import load_core_companies
    n_core = 0
    for c in load_core_companies(sector):
        code = c.get("code")
        if code and code not in seen:
            seen[code] = {"code": code, "name": c.get("name", ""),
                          "source": "core_company", "segment_hint": c.get("segment", "") or sec_name}
            n_core += 1
    for q in queries:
        r = search_cache.get_cached(q)
        if not r and provider:
            try:
                r = provider.search_with_ai_summary(q, max_results=8)
            except Exception as e:
                print(f"[valuation-lens] 板块搜索失败 ({q[:40]}): {e}", file=sys.stderr)
            if r and (r.get("results") or r.get("answer")):
                search_cache.set_cached(q, r)
        if not r:
            continue
        text = (r.get("answer") or "") + "\n" + "\n".join(
            (res.get("content") or "")[:700] for res in r.get("results", [])[:6]
        )
        for det in detector.detect_stocks_from_text(text):
            code = det.get("code")
            if code and code not in seen:
                seen[code] = {"code": code, "name": det.get("name", ""),
                              "source": "discovered", "segment_hint": sec_name}

    # 合并财联社热度发现（板块相关新闻里高频出现的标的）
    cls_hot = _candidates_from_cailianshe(sec_name, kps, days=days)
    for c in cls_hot:
        if c["code"] in seen:
            seen[c["code"]]["mention_count"] = c["mention_count"]  # Tavily 已发现，补热度
        else:
            seen[c["code"]] = c

    # 召回档案里该板块历史好标的（上次评分不错、本次搜索没挖到的）
    # 用 canon（与 _upsert_archive 的 sec_key 一致），否则 to_under(sector) 与档案写的 canonical key 对不上
    n_arc = 0
    for c in _recall_archive_candidates(canon):
        if c["code"] not in seen:
            seen[c["code"]] = c
            n_arc += 1
    print(f"[valuation-lens] 自动发现 {len(seen)} 只候选（板块={sec_name}，"
          f"核心公司 {n_core}，财联社热门 {len(cls_hot)}，档案召回 {n_arc}）", file=sys.stderr)
    return list(seen.values())


def _candidates_from_cailianshe(sec_name: str, kps: list, days: int = 14) -> List[dict]:
    """从财联社近期新闻发现板块热门标的（hermes latest_news.json + StockDetector）。

    复用 chain_agent 已接的财联社数据源（hermes cron 维护）。按板块关键词过滤新闻后，
    统计每只股票在相关新闻中的出现次数作为热度。days 限制新闻回看窗口（publish_time）。
    返回 [{code, name, source='cailianshe_hot', segment_hint, mention_count}]，按热度降序。
    """
    from chain_agent.discovery.stock_detector import StockDetector
    from collections import Counter

    try:
        data = json.loads(config.HERMES_NEWS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []
    news = data.get("news") or []
    if not news:
        return []

    cutoff = datetime.now() - timedelta(days=days)
    kws = [sec_name] + [k for k in (kps or []) if k]
    detector = StockDetector()
    mention: Counter = Counter()
    names: Dict[str, str] = {}
    for n in news:
        pt = n.get("publish_time", "") or ""
        if pt:  # 早于回看窗口的跳过；parse 失败则保留（不误删）
            try:
                if datetime.fromisoformat(pt.replace("Z", "")) < cutoff:
                    continue
            except Exception:
                pass
        text = " ".join([str(n.get("title", "")),
                         str(n.get("content", ""))[:800],
                         str(n.get("brief", ""))[:400]])
        if not any(kw in text for kw in kws):
            continue
        for det in detector.detect_stocks_from_text(text):
            code = det.get("code")
            if code:
                mention[code] += 1
                if not names.get(code):
                    names[code] = det.get("name", "")
    if not mention:
        return []
    top = mention.most_common(8)
    print(f"[valuation-lens] 财联社热度 top3: {top[:3]}", file=sys.stderr)
    return [{"code": code, "name": names.get(code, ""), "source": "cailianshe_hot",
             "segment_hint": sec_name, "mention_count": cnt} for code, cnt in top]


# ===== 2. 单标的搜索（稀缺 S / 前瞻 F / 供需 D）=====
# snippet（里程碑关键词锚定，跳过导航 boilerplate）在共享 chain_agent/collectors/snippet.py
def _valuation_search(provider, provider_name: Optional[str],
                      stock_name: str, code: str, year: int,
                      archive_entry: Optional[dict], days: int = 14) -> dict:
    """对单只股票搜 S/F/D evidence。

    - 24h 内跑过（档案 fresh）：复用档案 evidence_pool，跳过 Tavily
    - 否则：全新 Tavily S/F/D 搜索
    - 始终：拉财联社 per-stock 实时新闻，追加为 D 证据
    返回 {evidence, content_text, provider, used_archive, key_facts, prev, new_pool}。
    """
    evidence: Dict[str, dict] = {}
    chunks: List[str] = []
    new_pool: Dict[str, list] = {"S": [], "F": [], "D": []}
    now_iso = datetime.now().isoformat()
    used_archive = False
    key_facts = (archive_entry or {}).get("key_facts") or {}
    prev = None
    hist = (archive_entry or {}).get("score_history") or []
    if hist:
        prev = hist[-1]

    if _archive_is_fresh(archive_entry):
        used_archive = True
        pool = (archive_entry or {}).get("evidence_pool") or {}
        for prefix in ("S", "F", "D"):
            for i, item in enumerate((pool.get(prefix) or [])[:4], 1):
                eid = f"{prefix}{i}"
                txt = item.get("text", "")
                col = (item.get("collected_at") or "")[:10]
                evidence[eid] = {"title": f"[档案{col}]", "content": txt,
                                 "source": "archive", "url": ""}
                chunks.append(f"[{eid}] [档案{col}] {txt}")
    else:
        queries = {
            "S": f"{stock_name} 卡脖子 寡头 垄断 独家 不可替代 壁垒 护城河 市占率 CR3 {year}",
            "F": f"{stock_name} 在研 量产 突破 订单 产能扩张 第二增长曲线 技术路线 {year}",
            "D": f"{stock_name} 涨价 供需 缺货 产能利用率 国产替代 价格 {year}",
        }
        for prefix, q in queries.items():
            r = search_cache.get_cached(q)
            if not r and provider:
                try:
                    r = provider.search_with_ai_summary(q, max_results=5)
                except Exception as e:
                    print(f"[valuation-lens] 搜索失败 ({q[:40]}): {e}", file=sys.stderr)
                if r and (r.get("results") or r.get("answer")):
                    search_cache.set_cached(q, r)
            if not r:
                continue
            idx = 0
            if r.get("answer"):
                idx += 1
                eid = f"{prefix}{idx}"
                ans = r["answer"][:400]
                evidence[eid] = {"title": f"[AI摘要] {q}", "content": ans,
                                 "source": provider_name or "web", "url": ""}
                chunks.append(f"[{eid}] AI摘要 | {ans}")
                new_pool[prefix].append({"text": ans, "source": "tavily_ai", "collected_at": now_iso})
            for res in r.get("results", [])[:3]:
                idx += 1
                eid = f"{prefix}{idx}"
                title = res.get("title", "")
                content = _snippet(res.get("content") or "")
                evidence[eid] = {"title": title, "content": content,
                                 "source": provider_name or "web", "url": res.get("url", "")}
                chunks.append(f"[{eid}] {title} | {content}")
                new_pool[prefix].append({"text": f"{title} | {content}", "source": "tavily",
                                         "collected_at": now_iso})

    # 始终拉财联社 per-stock 实时新闻，追加为 D 证据（标"新增"= publish_time 晚于上次 last_run）
    last_run = (archive_entry or {}).get("last_run")
    for item in _cailianshe_per_stock(stock_name, code, limit=3, last_run=last_run, days=days):
        d_idx = len([k for k in evidence if k.startswith("D")]) + 1
        eid = f"D{d_idx}"
        txt = item.get("text", "")
        pt = (item.get("publish_time") or "")[:10]
        title = item.get("title", "")
        new_tag = " 新增" if item.get("is_new") else ""
        evidence[eid] = {"title": f"[财联社{pt}{new_tag}] {title}", "content": txt,
                         "source": "cailianshe", "url": ""}
        chunks.append(f"[{eid}] [财联社{pt}{new_tag}] {title} | {txt}")
        new_pool["D"].append({"text": f"{title} | {txt}", "source": "cailianshe", "collected_at": now_iso})

    return {"evidence": evidence, "content_text": "\n".join(chunks)[:6000],
            "provider": provider_name, "used_archive": used_archive,
            "key_facts": key_facts, "prev": prev, "new_pool": new_pool,
            "prev_pool": (archive_entry or {}).get("evidence_pool") or {}}


def search_all_candidates(candidates: List[dict], days: int = 14,
                          use_archive: bool = True) -> Dict[str, dict]:
    """并发对所有候选做 S/F/D 搜索。
    use_archive=False 时跳过档案复用（codes 显式探索模式，每次全新搜 Tavily）。"""
    provider, provider_name = _get_search_provider()
    year = datetime.now().year
    arc = _load_archive() if use_archive else {}
    print(f"[valuation-lens] 并发搜索 {len(candidates)} 只候选 "
          f"(provider={provider_name or 'none'}, use_archive={use_archive})", file=sys.stderr)

    results: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_valuation_search, provider, provider_name,
                          c["name"] or c["code"], c["code"], year,
                          arc.get(c["code"]) if use_archive else None, days): c["code"]
                for c in candidates if c.get("code")}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                results[code] = fut.result()
            except Exception as e:
                results[code] = {"evidence": {}, "content_text": "", "error": str(e)}
    n_arc = sum(1 for v in results.values() if v.get("used_archive"))
    print(f"[valuation-lens] 档案复用 {n_arc}/{len(results)} 只（24h内跳过 Tavily，财联社实时）",
          file=sys.stderr)
    return results
