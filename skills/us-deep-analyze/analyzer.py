"""
us-deep-analyze 主 pipeline（美股版）

基于 skills/deep-analyze/analyzer.py 改写：
- StockDetector → StockDetectorUS（ticker 正则 + us_stock_list.json）
- news_akshare → news_finnhub
- chain_agent.config.ECOSYSTEM_JSON → us_chain_agent.config.US_ECOSYSTEM_JSON
- chain_agent.config.STOCK_LIST_JSON → us_chain_agent.config.US_STOCK_LIST_JSON
- A 股 6 位代码 → 美股 2-5 位 ticker
- system prompt "A 股产业研究员" → "美股产业研究员"
- _segment_search 搜索词：去掉"国产替代/中国"，加"global market share/technology barrier"

chain 模式: decompose → per-segment search → bottleneck → score → report
stock 模式: identify company → decompose its chain → bottleneck → score (with peers) → verdict
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional

from chain_agent import config  # 复用 to_under / OUTPUT_DIR
from chain_agent.collectors.tavily_search import TavilySearch
from chain_agent.collectors.zhipu_search import ZhipuSearch
from chain_agent.collectors.snippet import snippet
from chain_agent.llm.client import get_llm_client
from chain_agent.llm.parse import json_from_llm as _json_from_llm

from us_chain_agent import config as us_config
from us_chain_agent.collectors import news_finnhub
from us_chain_agent.discovery.stock_detector_us import StockDetectorUS
from us_chain_agent.scoring.quotes_us import FinnhubQuoteProvider

from . import prompts


# ===== 工具 =====
# _json_from_llm 复用共享 chain_agent/llm/parse.py（import 别名，行为与原本地实现一致）
# snippet 复用共享 chain_agent/collectors/snippet.py（里程碑关键词锚定，跳过导航 boilerplate）


def _get_search_provider():
    """优先 Tavily，失败兜底智谱 web_search_pro，都失败返回 (None, None)。

    返回 (provider, name)：name ∈ {"tavily","zhipu",None}，供下游标记 evidence 来源用。
    """
    try:
        return TavilySearch(), "tavily"
    except Exception as e:
        print(f"[us-deep-analyze] Tavily 不可用: {e}，尝试智谱兜底", file=sys.stderr)
    if config.ZHIPU_API_KEY:
        try:
            return ZhipuSearch(), "zhipu"
        except Exception as e:
            print(f"[us-deep-analyze] 智谱不可用: {e}", file=sys.stderr)
    return None, None


# 模块级 detector 单例，避免每次 _segment_search 都重载 us_stock_list.json
_detector_singleton: Optional[StockDetectorUS] = None


def _detector() -> StockDetectorUS:
    global _detector_singleton
    if _detector_singleton is None:
        _detector_singleton = StockDetectorUS()
    return _detector_singleton


# 美股 ticker 正则：2-5 位大写字母
_TICKER_RE = re.compile(r"^[A-Z]{2,5}$")


def _resolve_leader_code(name: str, segment_hint: str = None) -> Optional[dict]:
    """从公司名解析美股 ticker。多匹配时按优先级挑选。

    优先级：
    1. 仅一个匹配 → 直接返回
    2. 精确名匹配（d["name"] == name）→ 第一个精确匹配
    3. segment_hint 能匹配 d["sector"]（to_under 后子串包含）→ 第一个板块匹配
    4. 兜底取第一个 + stderr 警告（可追溯）
    """
    detected = _detector().detect_stocks_from_text(name)
    if not detected:
        return None
    if len(detected) == 1:
        return detected[0]
    exact = [d for d in detected if d.get("name") == name]
    if exact:
        return exact[0]
    if segment_hint:
        hint = config.to_under(segment_hint)
        seg_match = [d for d in detected if hint and hint in (d.get("sector") or "")]
        if seg_match:
            return seg_match[0]
    print(f"[us-deep-analyze] [warn] {name!r} 多匹配 {len(detected)} 个 "
          f"({[d['code']+'/'+d['name'] for d in detected]})，取第一个 {detected[0]['code']}",
          file=sys.stderr)
    return detected[0]


def _llm_call(system: str, user: str) -> Optional[str]:
    client = get_llm_client()
    if client is None:
        print("[us-deep-analyze] LLM 不可用", file=sys.stderr)
        return None
    try:
        return client.synthesize(system, user)
    except Exception as e:
        print(f"[us-deep-analyze] LLM 调用失败: {e}", file=sys.stderr)
        return None


def _llm_call_meta(system: str, user: str) -> dict:
    """同 _llm_call，但额外返回 stop_reason 用于检测 max_tokens 截断。"""
    client = get_llm_client()
    if client is None:
        print("[us-deep-analyze] LLM 不可用", file=sys.stderr)
        return {"text": None, "stop_reason": None}
    try:
        return client.synthesize_with_meta(system, user)
    except Exception as e:
        print(f"[us-deep-analyze] LLM 调用失败: {e}", file=sys.stderr)
        return {"text": None, "stop_reason": None}


# ===== 1. 产业链拆解 =====
def _load_known_ecosystem(chain: str) -> str:
    """从 us_sector_ecosystem.json 拿已知结构作为 LLM 的提示"""
    try:
        ec = json.loads(us_config.US_ECOSYSTEM_JSON.read_text(encoding="utf-8"))
        # chain 可能是中文也可能是英文 key
        under = config.to_under(chain)
        node = ec.get(under) or ec.get(chain)
        if not node:
            return "(无已知数据)"
        return json.dumps(node, ensure_ascii=False, indent=2)
    except Exception:
        return "(无已知数据)"


def decompose_chain(chain: str) -> Optional[dict]:
    """让 LLM 把链拆成 5-8 个具体环节"""
    known = _load_known_ecosystem(chain)
    user = prompts.DECOMPOSE_USER_TEMPLATE.format(
        chain_name=chain, known_ecosystem=known
    )
    text = _llm_call(prompts.DECOMPOSE_SYSTEM, user)
    if not text:
        return None
    data = _json_from_llm(text)
    if not data or "segments" not in data:
        print(f"[us-deep-analyze] 拆解 JSON 解析失败，原文: {text[:200]}", file=sys.stderr)
        return None
    return data


# ===== 2. 单环节搜索（供需/技术壁垒/业绩/CR3）=====
def _segment_search(provider, provider_name: Optional[str], segment_name: str,
                    us_leaders: List[str], days: int) -> dict:
    """对单个环节发 4 条搜索查询 + 拉美股龙头 Finnhub 个股新闻。

    evidence_id 机制：
    - 每条搜索结果编 T1/T2/...（T 代表 Tavily 或 zhipu，统称 web 搜索）
    - 每条 Finnhub 新闻编 A1/A2/...
    - content_text 改成 "[T1] {title} | {content}" 格式
    - 返回 evidence dict: {id: {title, content, source, url}}
    """
    year = datetime.now().year
    # 英文搜索词（美股搜索结果更丰富）
    queries = [
        f"{segment_name} supply demand price capacity 2025",
        f"{segment_name} global market share technology barrier {year}",
        f"{segment_name} leader earnings orders market share",
        f"{segment_name} CR3 market share concentration {year}",
    ]

    evidence: Dict[str, dict] = {}
    web_chunks: List[str] = []
    t_idx = 0
    if provider:
        for q in queries:
            try:
                r = provider.search_with_ai_summary(q, max_results=5)
            except Exception as e:
                print(f"[us-deep-analyze] 搜索查询失败 ({q}): {e}", file=sys.stderr)
                continue
            if not r:
                continue
            # AI 摘要（仅 Tavily 有，智谱留空）
            if r.get("answer"):
                t_idx += 1
                eid = f"T{t_idx}"
                evidence[eid] = {
                    "title": f"[AI 摘要] {q}",
                    "content": r["answer"][:400],
                    "source": provider_name or "web",
                    "url": "",
                }
                web_chunks.append(f"[{eid}] AI 摘要 | {r['answer'][:400]}")
            for res in r.get("results", [])[:3]:
                t_idx += 1
                eid = f"T{t_idx}"
                title = res.get("title", "")
                content = snippet(res.get("content") or "")
                url = res.get("url", "")
                evidence[eid] = {
                    "title": title,
                    "content": content,
                    "source": provider_name or "web",
                    "url": url,
                }
                web_chunks.append(f"[{eid}] {title} | {content}")

    # 美股龙头 Finnhub 个股新闻
    leader_codes = []
    for name in (us_leaders or []):
        resolved = _resolve_leader_code(name, segment_hint=segment_name)
        if resolved:
            leader_codes.append(resolved["code"])
    leader_codes = leader_codes[:3]

    finnhub_news: dict = {}
    finnhub_chunks: List[str] = []
    if leader_codes:
        try:
            r = news_finnhub.collect_stock_news(leader_codes, days=days)
            finnhub_news = {
                "news_count": r.get("news_count", 0),
                "content_text": (r.get("content_text") or "")[:2000],
            }
            # 给每条 Finnhub 新闻编 A1/A2/...
            for a_idx, news in enumerate(r.get("news", [])[:8], start=1):
                eid = f"A{a_idx}"
                title = news.get("title", "")
                content = snippet(news.get("content") or "")
                pub = news.get("publish_time", "")
                evidence[eid] = {
                    "title": title,
                    "content": content,
                    "source": news.get("source", "finnhub"),
                    "url": news.get("url", ""),
                }
                finnhub_chunks.append(f"[{eid}] {pub} {title} | {content}")
        except Exception as e:
            finnhub_news = {"error": str(e)}

    chunks = web_chunks
    if finnhub_chunks:
        chunks.append(f"[Finnhub 新闻 {finnhub_news.get('news_count', 0)} 条]\n"
                      + "\n".join(finnhub_chunks))

    return {
        "segment": segment_name,
        "provider": provider_name,
        "tavily_count": len([k for k in evidence if k.startswith("T")]),
        "finnhub_news": finnhub_news,
        "evidence": evidence,
        "content_text": "\n".join(chunks),
    }


def _customer_search(provider, provider_name: Optional[str],
                     stock_name: str, stock_code: str) -> dict:
    """对单只美股发 2 条查询挖主要客户 + 客户营收占比。

    evidence_id 用 C1/C2/...（C 代表 Customer），与 _segment_search 的 T/A 区分。
    返回结构对齐 _segment_search：{evidence, content_text, provider}。
    """
    queries = [
        f"{stock_name} major customers top customers revenue concentration",
        f"{stock_name} largest customer revenue percentage customer dependency",
    ]

    evidence: Dict[str, dict] = {}
    chunks: List[str] = []
    c_idx = 0
    if provider:
        for q in queries:
            try:
                r = provider.search_with_ai_summary(q, max_results=5)
            except Exception as e:
                print(f"[us-deep-analyze] 客户搜索失败 ({q}): {e}", file=sys.stderr)
                r = None
            if not r:
                continue
            if r.get("answer"):
                c_idx += 1
                eid = f"C{c_idx}"
                evidence[eid] = {
                    "title": f"[AI 摘要] {q}",
                    "content": r["answer"][:400],
                    "source": provider_name or "web",
                    "url": "",
                }
                chunks.append(f"[{eid}] AI 摘要 | {r['answer'][:400]}")
            for res in r.get("results", [])[:3]:
                c_idx += 1
                eid = f"C{c_idx}"
                title = res.get("title", "")
                content = snippet(res.get("content") or "")
                url = res.get("url", "")
                evidence[eid] = {
                    "title": title,
                    "content": content,
                    "source": provider_name or "web",
                    "url": url,
                }
                chunks.append(f"[{eid}] {title} | {content}")

    return {
        "provider": provider_name,
        "evidence": evidence,
        "content_text": "\n".join(chunks),
        "customer_count": len(evidence),
    }


def search_all_segments(chain_data: dict, days: int) -> Dict[str, dict]:
    """并发对所有环节做搜索"""
    provider, provider_name = _get_search_provider()
    segments = chain_data.get("segments", [])
    print(f"[us-deep-analyze] 并发搜索 {len(segments)} 个环节 "
          f"(provider={provider_name or 'none'})", file=sys.stderr)

    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(_segment_search, provider, provider_name,
                      s["name"], s.get("us_leaders", []), days): s["name"]
            for s in segments
        }
        for fut in as_completed(futs):
            seg_name = futs[fut]
            try:
                results[seg_name] = fut.result()
            except Exception as e:
                results[seg_name] = {"segment": seg_name, "content_text": "",
                                     "error": str(e)}
    # 按 segments 原序重建 dict，保证喂给 LLM 时顺序稳定可复现
    ordered = {}
    for s in segments:
        name = s["name"]
        ordered[name] = results.get(name, {
            "segment": name, "content_text": "", "error": "missing"
        })
    return ordered


# ===== 3. 卡脖子判断 =====
def _check_all_sources_failed(search_results: dict) -> bool:
    """所有环节的 web 搜索和 Finnhub 新闻都为空 → True"""
    total_web = sum(d.get("tavily_count", 0) for d in search_results.values())
    total_ak = sum((d.get("finnhub_news") or {}).get("news_count", 0)
                   for d in search_results.values())
    return total_web == 0 and total_ak == 0


def identify_bottlenecks(chain_data: dict, search_results: dict) -> Optional[dict]:
    """让 LLM 判断每环节的卡脖子程度。要求引用 evidence_id + 提取具体数字。"""
    segments_brief = []
    for s in chain_data.get("segments", []):
        segments_brief.append({
            "name": s["name"],
            "role": s.get("role"),
            "global_leaders": s.get("global_leaders", []),
            "us_leaders": s.get("us_leaders", []),
            "concentration": s.get("concentration"),
            "us_share": s.get("us_share"),
            "tech_barrier": s.get("tech_barrier"),
        })

    # 把 evidence 全文（带 ID）注入 prompt，让 LLM 必须引用 ID
    search_summary = "\n\n".join(
        f"## {seg}\n{data.get('content_text', '')[:1500]}"
        for seg, data in search_results.items()
    )

    user = prompts.BOTTLENECK_USER_TEMPLATE.format(
        chain_name=chain_data.get("chain_name", ""),
        segments_json=json.dumps(segments_brief, ensure_ascii=False, indent=2),
        search_data=search_summary,
    )
    text = _llm_call(prompts.BOTTLENECK_SYSTEM, user)
    if not text:
        return None
    data = _json_from_llm(text)
    if not data:
        print(f"[us-deep-analyze] 卡脖子 JSON 解析失败: {text[:200]}", file=sys.stderr)
        return None
    return data


# ===== 4. 三维评分 =====
def _candidate_sort_key(c: dict, bottleneck_segments: dict, quotes: dict) -> tuple:
    """候选股排序键（升序 sort，越小越靠前）。

    优先级（高 → 低）：
    1. force_include 强制包含（source='force_include'）
    2. us_leaders 拆解阶段龙头（source='us_leaders'）
    3. news_discovery 新闻发现的活跃标的
    4. 所处环节是卡脖子环节
    5. 新闻命中次数多
    6. 市值大（流动性更好）
    """
    seg_bn = bottleneck_segments.get(c.get("segment", ""), {})
    is_bn = 1 if seg_bn.get("is_bottleneck") else 0
    source_rank = {"force_include": 0, "us_leaders": 1, "news_discovery": 2}.get(c.get("source"), 3)
    news_hits = c.get("news_hits", 0)
    mktcap = (quotes.get(c["code"], {}) or {}).get("market_cap") or 0
    # 升序 sort：source_rank 越小越靠前；is_bn/news_hits/mktcap 用负号让大的靠前
    return (source_rank, -is_bn, -news_hits, -mktcap)


def score_candidates(chain_data: dict, bottleneck_data: dict,
                     search_results: dict, top_n: int,
                     force_include_codes: Optional[List[str]] = None,
                     force_include_segment: Optional[str] = None,
                     quotes: Optional[Dict[str, dict]] = None) -> Optional[dict]:
    """对候选美股标的做三维评分。

    候选池来源：
    1. force_include_codes（stock 模式强制塞入目标股，优先级最高）
    2. 拆解阶段 LLM 给出的 us_leaders
    3. 各环节搜索文本中反向捞到的美股 ticker
    排序后截断到 top_n*2，再注入 PE/市值/涨跌幅喂给 LLM。
    """
    candidates = []
    seen_codes = set()
    detector = _detector()
    quotes = quotes or {}

    # 来源 0：force_include_codes（stock 模式专用，优先级最高）
    for code in (force_include_codes or []):
        if code in seen_codes:
            continue
        # 反查 name
        try:
            stock_list = json.loads(us_config.US_STOCK_LIST_JSON.read_text(encoding="utf-8"))
            stocks = stock_list.get("stocks", stock_list)
            info = stocks.get(code)
            name = info.get("name", "") if isinstance(info, dict) else str(info)
        except Exception:
            name = ""
        seen_codes.add(code)
        candidates.append({
            "code": code,
            "name": name,
            "segment": force_include_segment or "",
            "role": "",
            "source": "force_include",
        })

    # 来源 1：拆解阶段 LLM 给出的 us_leaders（每环节 3-5 家）
    for s in chain_data.get("segments", []):
        for name in s.get("us_leaders", []):
            resolved = _resolve_leader_code(name, segment_hint=s.get("name"))
            if not resolved:
                continue
            code = resolved["code"]
            if code in seen_codes:
                continue
            seen_codes.add(code)
            candidates.append({
                "code": code,
                "name": resolved["name"],
                "segment": s["name"],
                "role": s.get("role", ""),
                "source": "us_leaders",
            })

    # 来源 2：从各环节搜索文本中反向捞美股 ticker
    for seg_name, seg_data in search_results.items():
        text = seg_data.get("content_text", "")
        if not text:
            continue
        detected = detector.detect_stocks_from_text(text)
        for d in detected:
            if d["code"] in seen_codes:
                continue
            seen_codes.add(d["code"])
            candidates.append({
                "code": d["code"],
                "name": d["name"],
                "segment": seg_name,
                "role": "",
                "source": "news_discovery",
            })

    if not candidates:
        return {"candidates": [], "note": "no candidates detected from us_leaders or news"}

    # 截断前按优先级排序
    bottleneck_segments = {
        s.get("name"): s for s in bottleneck_data.get("segments", [])
    }
    # 统计 news_hits：在所有 search_results content_text 中出现次数
    all_text = "\n".join(d.get("content_text", "") for d in search_results.values())
    for c in candidates:
        name = c.get("name", "")
        code = c.get("code", "")
        if name and len(name) >= 2:
            c["news_hits"] = all_text.count(name)
        else:
            c["news_hits"] = 0
        # 注入 PE/市值/涨跌幅
        q = quotes.get(c["code"], {}) or {}
        c["pe"] = q.get("pe")
        c["market_cap"] = q.get("market_cap")
        c["change_pct"] = q.get("change_pct")

    candidates.sort(key=lambda c: _candidate_sort_key(c, bottleneck_segments, quotes))
    candidates = candidates[:top_n * 2]

    bottleneck_summary = json.dumps({
        "top_bottlenecks": bottleneck_data.get("top_bottlenecks", []),
        "segments": [
            {"name": s.get("name"), "score": s.get("bottleneck_score"),
             "is_bottleneck": s.get("is_bottleneck")}
            for s in bottleneck_data.get("segments", [])
        ]
    }, ensure_ascii=False, indent=2)

    search_summary = "\n\n".join(
        f"## {seg}\n{data.get('content_text', '')[:1500]}"
        for seg, data in search_results.items()
    )

    # ===== 分批评分（防 LLM max_tokens 截断）=====
    SCORE_BATCH_SIZE = int(os.environ.get("DEEP_ANALYZE_SCORE_BATCH", "8"))
    if SCORE_BATCH_SIZE < 2:
        SCORE_BATCH_SIZE = 2

    batches = [candidates[i:i + SCORE_BATCH_SIZE]
               for i in range(0, len(candidates), SCORE_BATCH_SIZE)]
    print(f"[us-deep-analyze] 评分分批: {len(candidates)} 只候选 / {len(batches)} 批 "
          f"(batch_size={SCORE_BATCH_SIZE})", file=sys.stderr)

    out_cands: list = []
    raw_llm_snippets: list = []
    any_batch_truncated = False

    def _call_scoring_batch(batch):
        """对一批候选调 LLM 评分。返回 (batch_out, fail_raw, truncated)。
        batch_out 空=失败（无响应或 JSON 解析失败）；fail_raw 为失败时的原文片段。"""
        user = prompts.SCORING_USER_TEMPLATE.format(
            chain_name=chain_data.get("chain_name", ""),
            bottleneck_summary=bottleneck_summary,
            candidates=json.dumps(batch, ensure_ascii=False, indent=2),
            search_data=search_summary,
            top_n=len(batch),
        )
        meta = _llm_call_meta(prompts.SCORING_SYSTEM, user)
        text = meta.get("text") or ""
        truncated = meta.get("stop_reason") == "max_tokens"
        if not text:
            return [], "", False
        data = _json_from_llm(text)
        if not data:
            return [], text[:500], truncated
        # 规整成 list[dict]
        if isinstance(data, list):
            batch_out = data
        elif isinstance(data, dict):
            batch_out = data.get("candidates") or data.get("segments") or []
            if not batch_out and data.get("stock_code"):
                batch_out = [data]
        else:
            batch_out = []
        return batch_out, "", truncated

    for idx, batch in enumerate(batches, 1):
        batch_out, fail_raw, truncated = _call_scoring_batch(batch)
        if truncated:
            any_batch_truncated = True
        if batch_out:
            print(f"[us-deep-analyze] 批 {idx}/{len(batches)} 解析出 {len(batch_out)} 只",
                  file=sys.stderr)
            out_cands.extend(batch_out)
            continue
        # 整批失败 → 逐只重试（小批次更易成功，规避 max_tokens 截断 / 单只畸形输出）
        reason = "无响应" if not fail_raw else "JSON解析失败"
        print(f"[us-deep-analyze] [warn] 批 {idx}/{len(batches)} {reason}，逐只重试",
              file=sys.stderr)
        if fail_raw:
            raw_llm_snippets.append(fail_raw)
        for c in batch:
            single_out, fr2, tr2 = _call_scoring_batch([c])
            if tr2:
                any_batch_truncated = True
            if single_out:
                out_cands.extend(single_out)
                print(f"[us-deep-analyze]   重试 {c.get('code')} 成功", file=sys.stderr)
            else:
                # 兜底：保留候选，scores 留空（下游渲染为 '-'）
                out_cands.append({**c, "stock_code": c["code"]})
                if fr2:
                    raw_llm_snippets.append(fr2)
                print(f"[us-deep-analyze]   重试 {c.get('code')} 仍失败，留空", file=sys.stderr)

    data = {"candidates": out_cands}
    if raw_llm_snippets:
        data["raw_llm_partial"] = "\n---\n".join(raw_llm_snippets)[:2000]
    if any_batch_truncated:
        data["batch_truncated"] = True

    # 把输入候选的 pe/market_cap/change_pct/news_hits/source 合并回 LLM 输出
    # 同时校验 LLM 输出的 stock_code 是否与 company 名一致
    input_by_code = {c["code"]: c for c in candidates}
    input_by_name = {c["name"]: c for c in candidates if c.get("name")}
    for oc in out_cands:
        llm_code = oc.get("stock_code") or oc.get("code")
        llm_name = oc.get("company") or oc.get("name") or ""
        # 校验：LLM 给的 code 在输入候选里，但 name 对不上 → 试着按 name 反查正确 code
        if llm_code in input_by_code:
            input_name = input_by_code[llm_code].get("name", "")
            name_mismatch = (llm_name and input_name
                             and llm_name != input_name
                             and llm_name not in input_name
                             and input_name not in llm_name)
            if name_mismatch and llm_name in input_by_name:
                correct = input_by_name[llm_name]
                print(f"[us-deep-analyze] [warn] LLM 代码错配纠正: "
                      f"{llm_name} {llm_code}→{correct['code']} "
                      f"(原 {llm_code} 实为 {input_name})", file=sys.stderr)
                llm_code = correct["code"]
        elif llm_name in input_by_name:
            correct = input_by_name[llm_name]
            print(f"[us-deep-analyze] [warn] LLM 代码补全: "
                  f"{llm_name} {llm_code}→{correct['code']}", file=sys.stderr)
            llm_code = correct["code"]
        # 用校验后的 code 合并输入字段
        if not llm_code or llm_code not in input_by_code:
            continue
        src = input_by_code[llm_code]
        oc["stock_code"] = llm_code
        oc["code"] = llm_code
        oc.setdefault("pe", src.get("pe"))
        oc.setdefault("market_cap", src.get("market_cap"))
        oc.setdefault("change_pct", src.get("change_pct"))
        oc.setdefault("news_hits", src.get("news_hits"))
        oc.setdefault("source", src.get("source"))
        if not oc.get("segment"):
            oc["segment"] = src.get("segment", "")
    return data


# ===== 主入口：chain 模式 =====
def analyze_chain(chain: str, days: int = 14, top_n: int = 8,
                  force_include_codes: Optional[List[str]] = None,
                  force_include_segment: Optional[str] = None) -> dict:
    print(f"[us-deep-analyze] === chain 模式: {chain} (days={days}, top_n={top_n}"
          f"{', force_include=' + str(force_include_codes) if force_include_codes else ''}) ===",
          file=sys.stderr)

    chain_data = decompose_chain(chain)
    if not chain_data:
        return {"error": "产业链拆解失败（LLM 不可用或返回格式错误）", "chain": chain}

    print(f"[us-deep-analyze] 拆出 {len(chain_data.get('segments', []))} 个环节",
          file=sys.stderr)

    search_results = search_all_segments(chain_data, days)

    all_failed = _check_all_sources_failed(search_results)
    data_quality = "degraded" if all_failed else "ok"

    bottleneck_data = identify_bottlenecks(chain_data, search_results) or {}
    print(f"[us-deep-analyze] 卡脖子环节: {bottleneck_data.get('top_bottlenecks', [])}",
          file=sys.stderr)

    # 评分前拉 PE/市值/涨跌幅注入候选
    quotes: Dict[str, dict] = {}
    try:
        candidate_codes = list(force_include_codes or [])
        for s in chain_data.get("segments", []):
            for name in s.get("us_leaders", []):
                resolved = _resolve_leader_code(name, segment_hint=s.get("name"))
                if resolved:
                    candidate_codes.append(resolved["code"])
        if candidate_codes:
            candidate_codes = list(dict.fromkeys(candidate_codes))  # 去重保序
            quotes = FinnhubQuoteProvider().get_quotes(candidate_codes) or {}
            print(f"[us-deep-analyze] 拉到 {len(quotes)} 只候选股的 PE/市值", file=sys.stderr)
    except Exception as e:
        print(f"[us-deep-analyze] 拉行情失败（PE/市值将为 null）: {e}", file=sys.stderr)

    scoring_data = score_candidates(
        chain_data, bottleneck_data, search_results, top_n,
        force_include_codes=force_include_codes,
        force_include_segment=force_include_segment,
        quotes=quotes,
    ) or {}

    return {
        "mode": "chain",
        "market": "us",
        "chain_name": chain_data.get("chain_name", chain),
        "run_time": datetime.now().isoformat(),
        "days": days,
        "data_quality": data_quality,
        "chain": chain_data,
        "bottleneck": bottleneck_data,
        "scoring": scoring_data,
        "search_stats": {
            seg: {"tavily_count": d.get("tavily_count", 0),
                  "provider": d.get("provider"),
                  "finnhub_news_count": (d.get("finnhub_news") or {}).get("news_count", 0)}
            for seg, d in search_results.items()
        },
    }


# ===== 主入口：stock 模式 =====
def analyze_stock(stock_input: str, days: int = 14) -> dict:
    """stock_input 可以是美股 ticker 或公司名"""
    print(f"[us-deep-analyze] === stock 模式: {stock_input} ===", file=sys.stderr)

    # 1. 定位公司
    # ticker 分支：大小写归一化（用户可能输入小写 aaoi）
    _ticker_norm = stock_input.upper()
    if _TICKER_RE.match(_ticker_norm):
        stock_list = json.loads(us_config.US_STOCK_LIST_JSON.read_text(encoding="utf-8"))
        stocks = stock_list.get("stocks", stock_list)
        info = stocks.get(_ticker_norm)
        if not info:
            return {"error": f"未在美股名单中找到 {_ticker_norm}"}
        stock_code = _ticker_norm
        stock_name = info.get("name", "") if isinstance(info, dict) else str(info)
    else:
        # 公司名 → 用 _resolve_leader_code 反查
        resolved = _resolve_leader_code(stock_input)
        if not resolved:
            return {"error": f"无法识别美股: {stock_input}"}
        stock_code = resolved["code"]
        stock_name = resolved["name"]

    print(f"[us-deep-analyze] 定位: {stock_name}({stock_code})", file=sys.stderr)

    # 2. 让 LLM 判断公司主营 + 所属产业链 + 环节
    identify_prompt = f"""请用一行 JSON 回答（不要代码块、不要解释）：
公司「{stock_name}（{stock_code}）」的主营业务、所属产业链（给中文链名，如"GPU"、"AI 云计算"、"消费电子"）、所处具体环节、环节角色(upstream/midstream/downstream)。

格式：{{"business":"...","chain_name":"...","segment":"...","role":"..."}}"""
    text = _llm_call("你是一位美股产业研究员，回答要简短准确。所有分析输出请用简体中文。", identify_prompt)
    company_info = _json_from_llm(text) if text else {}
    if not company_info:
        return {"error": f"无法定位 {stock_name} 的产业链", "raw_llm": text}

    chain_name = company_info.get("chain_name", "")
    segment = company_info.get("segment", "")
    print(f"[us-deep-analyze] 所属: {chain_name} / {segment}", file=sys.stderr)

    # 3. 跑 chain pipeline，强制把目标股塞进候选池
    chain_result = analyze_chain(
        chain_name, days=days, top_n=8,
        force_include_codes=[stock_code],
        force_include_segment=segment,
    )
    if "error" in chain_result:
        return {**chain_result,
                "mode": "stock", "market": "us",
                "stock_code": stock_code, "stock_name": stock_name,
                "company_info": company_info}

    # 4. 生成单股判断报告
    bottleneck_text = json.dumps(chain_result.get("bottleneck", {}), ensure_ascii=False, indent=2)
    scoring_text = json.dumps(chain_result.get("scoring", {}), ensure_ascii=False, indent=2)

    # 该公司专属搜索数据
    provider, provider_name = _get_search_provider()
    company_search = _segment_search(provider, provider_name, segment, [stock_name], days)

    # 客户结构搜索：挖主要客户 + 客户营收占比（C1/C2/... evidence）
    customer_search = _customer_search(provider, provider_name, stock_name, stock_code)
    print(f"[us-deep-analyze] 客户搜索: 拉到 {customer_search.get('customer_count', 0)} 条 evidence",
          file=sys.stderr)

    user = prompts.STOCK_VERDICT_USER_TEMPLATE.format(
        stock_name=stock_name, stock_code=stock_code,
        business=company_info.get("business", ""),
        chain_name=chain_name, segment=segment,
        role=company_info.get("role", ""),
        bottleneck_text=bottleneck_text,
        search_data=company_search.get("content_text", "")[:3000],
        customer_data=customer_search.get("content_text", "")[:2000],
        scoring_text=scoring_text,
    )
    verdict_md = _llm_call(prompts.STOCK_VERDICT_SYSTEM, user) or "(LLM 不可用，无法生成判断)"

    return {
        "mode": "stock",
        "market": "us",
        "stock_code": stock_code,
        "stock_name": stock_name,
        "company_info": company_info,
        "chain_analysis": chain_result,
        "verdict_md": verdict_md,
        "data_quality": chain_result.get("data_quality", "ok"),
        "run_time": datetime.now().isoformat(),
    }
