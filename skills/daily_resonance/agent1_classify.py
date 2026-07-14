"""
Agent 1: 事件分类与板块映射

将财联社新闻通过关键词匹配映射到30个板块，
推断事件类型和情绪方向。

Step 4 (LLM兜底): 关键词无法匹配的新闻，用LLM批量分类。
"""
import json
import re
import sys
from typing import Optional

from .data import load_keywords, load_stock_list, load_ecosystem, get_sector_name
from .config import (
    LLM_PROVIDER, LLM_MODEL, LLM_MAX_TOKENS, LLM_TEMPERATURE,
)

# ── 事件类型关键词 ──────────────────────────────────

EVENT_TYPE_PATTERNS = {
    "policy": [
        "政策", "印发", "出台", "鼓励", "支持", "规划", "纲要",
        "发改委", "工信部", "国务院", "商务部", "政治局",
    ],
    "technology": [
        "突破", "研发", "量产", "交付", "技术", "创新", "首发",
        "全球首款", "自主研制", "国产化", "替代",
    ],
    "earnings": [
        "业绩", "营收", "利润", "财报", "净利润", "预增", "预喜",
        "亏损", "扭亏", "同比增长", "环比增长",
    ],
    "capacity": [
        "扩产", "投资", "产能", "项目", "投产", "开工",
        "建设", "扩建", "新建",
    ],
    "order": [
        "订单", "中标", "合同", "采购", "签约", "供货", "交付",
    ],
    "supply_demand": [
        "涨价", "降价", "供需", "紧缺", "短缺", "过剩",
        "涨价", "降价", "供不应求", "供过于求",
    ],
}

SENTIMENT_POSITIVE = [
    "突破", "增长", "创新高", "超预期", "景气", "利好",
    "大涨", "涨停", "回升", "复苏", "加速",
]

SENTIMENT_NEGATIVE = [
    "下跌", "亏损", "风险", "下滑", "放缓", "收紧",
    "利空", "大跌", "跌停", "违约", "危机", "萎缩",
]


def classify_events(news_list: list[dict]) -> dict:
    """
    主入口：将新闻列表分类映射到板块。

    Step 1-3: 关键词匹配 + 个股关联 + 事件类型推断
    Step 4: LLM兜底（关键词无法匹配的新闻）

    返回:
        {
            "sector_key": {
                "name": str,
                "events": [
                    {
                        "news_id": str,
                        "title": str,
                        "sentiment": int,   # +1 / 0 / -1
                        "event_type": str,   # policy/technology/earnings/...
                        "importance": float,  # 归一化 [0, 1]
                        "stock_codes": list[str],
                    }
                ],
                "stats": {
                    "total": int,
                    "positive": int,
                    "negative": int,
                    "event_types": set[str],
                    "importance_sum": float,
                }
            }
        }
    """
    keywords = load_keywords()
    stock_list = load_stock_list()
    ecosystem = load_ecosystem()

    if not keywords:
        print("[Agent 1] ⚠️ 未加载到板块关键词", file=sys.stderr)
        return {}

    # 构建反向索引：keyword -> sector_key
    kw_to_sector = _build_keyword_index(keywords)

    # 按新闻ID去重（同一条新闻可能在不同时段被采集）
    seen_ids = set()
    unique_news = []
    for n in news_list:
        nid = n.get("id", "")
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            unique_news.append(n)

    # 每条新闻 -> 映射到板块
    sector_events: dict[str, dict] = {}

    # ── 收集LLM兜底候选（关键词无法匹配的新闻） ──
    llm_candidates = []

    for news in unique_news:
        title = news.get("title", "")
        content = news.get("content", "")
        text = (title + " " + content).lower()

        # Step 1: 关键词匹配 -> 找到所属板块
        matched_sectors = _match_sectors(text, kw_to_sector, keywords)

        # Step 2: 个股代码关联 -> 补充板块匹配
        stock_codes = news.get("stock_codes", [])
        if not stock_codes:
            stock_codes = _extract_stock_codes(text, stock_list)
        else:
            # 已有代码，检查是否对应到板块
            for code in stock_codes:
                stock_entry = stock_list.get(code, {})
                if isinstance(stock_entry, dict):
                    stock_name = stock_entry.get("name", "")
                else:
                    stock_name = str(stock_entry)
                if stock_name:
                    name_sectors = _match_sectors(stock_name.lower(), kw_to_sector, keywords)
                    matched_sectors.update(name_sectors)

        if not matched_sectors:
            # Step 4 候选：关键词无法匹配，留给LLM兜底
            llm_candidates.append(news)
            continue  # 暂不处理，等LLM兜底

        # Step 3: 事件类型推断
        event_type = _infer_event_type(title + " " + content)

        # 情绪判断
        sentiment = _infer_sentiment(title + " " + content)

        # 重要性归一化
        importance_raw = news.get("importance", 0)
        if isinstance(importance_raw, (int, float)):
            importance = min(importance_raw / 500.0, 1.0)
        else:
            importance = 0.0

        # 构建事件对象
        event = {
            "news_id": news.get("id", ""),
            "title": title,
            "content_snippet": content[:200] if content else "",
            "sentiment": sentiment,
            "event_type": event_type,
            "importance": importance,
            "level": news.get("level", "C"),
            "stock_codes": stock_codes,
        }

        # 写入每个匹配板块
        for sk in matched_sectors:
            if sk not in sector_events:
                sector_info = ecosystem.get(sk, {})
                sector_events[sk] = {
                    "name": sector_info.get("name", sk),
                    "events": [],
                    "stats": {"total": 0, "positive": 0, "negative": 0,
                              "event_types": set(), "importance_sum": 0.0},
                }
            sector_events[sk]["events"].append(event)
            sector_events[sk]["stats"]["total"] += 1
            if sentiment > 0:
                sector_events[sk]["stats"]["positive"] += 1
            elif sentiment < 0:
                sector_events[sk]["stats"]["negative"] += 1
            sector_events[sk]["stats"]["event_types"].add(event_type)
            sector_events[sk]["stats"]["importance_sum"] += importance

    # ── Step 4: LLM兜底 ──
    if llm_candidates:
        llm_mappings = _llm_classify_unmatched(llm_candidates, ecosystem)
        for news, sector_matches in llm_mappings:
            title = news.get("title", "")
            content = news.get("content", "")
            stock_codes = news.get("stock_codes", [])
            if not stock_codes:
                stock_codes = _extract_stock_codes(
                    (title + " " + content).lower(), stock_list
                )

            event_type = _infer_event_type(title + " " + content)
            sentiment = _infer_sentiment(title + " " + content)
            importance_raw = news.get("importance", 0)
            if isinstance(importance_raw, (int, float)):
                importance = min(importance_raw / 500.0, 1.0)
            else:
                importance = 0.0

            event = {
                "news_id": news.get("id", ""),
                "title": title,
                "content_snippet": content[:200] if content else "",
                "sentiment": sentiment,
                "event_type": event_type,
                "importance": importance,
                "level": news.get("level", "C"),
                "stock_codes": stock_codes,
                "llm_fallback": True,
            }

            for sk in sector_matches:
                if sk not in sector_events:
                    sector_info = ecosystem.get(sk, {})
                    sector_events[sk] = {
                        "name": sector_info.get("name", sk),
                        "events": [],
                        "stats": {"total": 0, "positive": 0, "negative": 0,
                                  "event_types": set(), "importance_sum": 0.0},
                    }
                sector_events[sk]["events"].append(event)
                sector_events[sk]["stats"]["total"] += 1
                if sentiment > 0:
                    sector_events[sk]["stats"]["positive"] += 1
                elif sentiment < 0:
                    sector_events[sk]["stats"]["negative"] += 1
                sector_events[sk]["stats"]["event_types"].add(event_type)
                sector_events[sk]["stats"]["importance_sum"] += importance

        print(f"[Agent 1] LLM兜底: {len(llm_candidates)} 条候选, "
              f"{sum(1 for _, m in llm_mappings if m)} 条映射成功",
              file=sys.stderr)

    # 统计信息中的 set -> list（JSON可序列化）
    for sk in sector_events:
        sector_events[sk]["stats"]["event_types"] = list(
            sector_events[sk]["stats"]["event_types"]
        )

    total_mapped = sum(v["stats"]["total"] for v in sector_events.values())
    print(f"[Agent 1] 新闻总数: {len(unique_news)}, 映射到板块: {total_mapped} 条, "
          f"涉及 {len(sector_events)} 个板块", file=sys.stderr)

    return sector_events


# ── LLM兜底 ──────────────────────────────────────────

LLM_BATCH_SIZE = 50      # 每批最多50条
LLM_MAX_CANDIDATES = 100  # 每天最多100条需要LLM兜底


def _llm_classify_unmatched(
    candidates: list[dict],
    ecosystem: dict,
) -> list[tuple[dict, list[str]]]:
    """
    用LLM批量分类关键词无法匹配的新闻。

    返回: [(news, [sector_key, ...]), ...]
           sector_key为空列表表示LLM也认为不归属任何板块。
    """
    if not candidates:
        return [(n, []) for n in candidates]

    # 限流：最多处理LLM_MAX_CANDIDATES条
    candidates = candidates[:LLM_MAX_CANDIDATES]

    # 构建板块列表供LLM选择
    sector_list = []
    for sk, info in ecosystem.items():
        if sk == "metadata":
            continue
        name = info.get("name", sk)
        sector_list.append(f"{sk}: {name}")
    sector_names = "\n".join(f"- {s}" for s in sector_list)

    results: list[tuple[dict, list[str]]] = []

    # 分批处理
    for batch_start in range(0, len(candidates), LLM_BATCH_SIZE):
        batch = candidates[batch_start:batch_start + LLM_BATCH_SIZE]
        batch_mappings = _llm_classify_batch(batch, sector_names, ecosystem)
        results.extend(batch_mappings)

    return results


def _llm_classify_batch(
    batch: list[dict],
    sector_names: str,
    ecosystem: dict,
) -> list[tuple[dict, list[str]]]:
    """对一批新闻调用LLM进行分类"""
    try:
        from chain_agent.llm.client import get_llm_client
        client = get_llm_client()
    except ImportError:
        print("[Agent 1 LLM] chain_agent.llm.client 不可用，跳过LLM兜底",
              file=sys.stderr)
        return [(n, []) for n in batch]

    # 构建新闻列表
    news_items = []
    for i, n in enumerate(batch):
        title = n.get("title", "")
        content = n.get("content", "")[:100]
        news_items.append(f"{i+1}. [{n.get('level','C')}] {title}"
                          f"{' | ' + content if content else ''}")

    news_text = "\n".join(news_items)

    system_prompt = ("你是一位专业的A股行业板块分类专家。"
                     "只将明确涉及A股特定产业链的新闻映射到板块，"
                     "不相关的新闻输出none。")

    prompt = f"""你是一位专业的A股行业板块分类专家。请判断以下新闻各自对应哪个板块。

## 可选板块
{sector_names}

## 新闻列表
{news_text}

请为每条新闻判断是否属于以上某个板块。如果明确属于某板块，输出板块key（如 optical_module）。
如果新闻与A股板块无关（如国际政治、港股IPO、宏观经济政策等），输出 "none"。

输出格式（JSON数组，每项一条）：
[
  {{"index": 1, "sectors": ["optical_module"]}},
  {{"index": 2, "sectors": ["none"]}},
  ...
]

要求：
1. 每条新闻至少选一个板块，最多选2个
2. 仅当新闻内容明确涉及A股特定行业/产业链时才映射
3. 宏观政策（利率、降准、财政政策等）输出 "none"
4. 非A股公司新闻（港股、美股、国际公司）输出 "none"
5. 地缘政治、军事冲突输出 "none"
6. 如果某个新闻可以映射到多个板块，列出所有匹配的"""

    try:
        result = client.synthesize(
            system=system_prompt,
            user=prompt,
            temperature=LLM_TEMPERATURE,
        )
    except Exception as e:
        print(f"[Agent 1 LLM] LLM调用失败: {e}", file=sys.stderr)
        return [(n, []) for n in batch]

    if not result:
        print("[Agent 1 LLM] LLM返回空结果", file=sys.stderr)
        return [(n, []) for n in batch]

    # 解析JSON
    try:
        from chain_agent.llm.parse import json_from_llm
        parsed = json_from_llm(result)
    except ImportError:
        # 简单回退
        parsed = _simple_parse_json(result)

    if not isinstance(parsed, list):
        print(f"[Agent 1 LLM] 解析结果非数组: {type(parsed)}", file=sys.stderr)
        return [(n, []) for n in batch]

    # 构建返回
    mapping = {item.get("index", i + 1): item.get("sectors", ["none"])
               for i, item in enumerate(parsed)
               if isinstance(item, dict)}

    result_list = []
    for i, n in enumerate(batch):
        idx = i + 1
        sectors = mapping.get(idx, ["none"])
        # 过滤掉"none"
        valid = [s for s in sectors if s != "none" and s in ecosystem]
        result_list.append((n, valid))

    return result_list


def _simple_parse_json(text: str) -> list:
    """简单JSON解析回退"""
    import re
    try:
        import json
        # 尝试提取JSON数组
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []
    except Exception:
        return []


# ── 内部函数 ──────────────────────────────────────────

def _build_keyword_index(keywords: dict) -> dict:
    """
    构建 keyword -> [sector_key, ...] 的反向索引
    """
    kw_to_sector = {}
    for sector_key, kw_list in keywords.items():
        if not isinstance(kw_list, list):
            continue
        for kw in kw_list:
            kw_lower = kw.lower().strip()
            if kw_lower:
                if kw_lower not in kw_to_sector:
                    kw_to_sector[kw_lower] = []
                kw_to_sector[kw_lower].append(sector_key)
    return kw_to_sector


def _match_sectors(text: str, kw_to_sector: dict,
                   keywords: dict) -> set:
    """用关键词匹配板块，返回匹配到的板块key集合"""
    matched = set()
    for kw, sectors in kw_to_sector.items():
        if kw in text:
            for sk in sectors:
                matched.add(sk)
    return matched


def _extract_stock_codes(text: str, stock_list: dict) -> list[str]:
    """从文本中提取6位数字股票代码"""
    codes = re.findall(r'\b(6\d{5}|3\d{5}|0\d{5})\b', text)
    # 验证代码是否在A股列表中
    valid = [c for c in codes if c in stock_list]
    return valid


def _infer_event_type(text: str) -> str:
    """推断事件类型"""
    text_lower = text.lower()
    scores = {}
    for etype, patterns in EVENT_TYPE_PATTERNS.items():
        score = sum(1 for p in patterns if p.lower() in text_lower)
        if score > 0:
            scores[etype] = score

    if not scores:
        return "general"
    return max(scores, key=scores.get)


def _infer_sentiment(text: str) -> int:
    """推断情绪方向：+1 正面, -1 负面, 0 中性"""
    text_lower = text.lower()
    pos_score = sum(1 for w in SENTIMENT_POSITIVE if w in text_lower)
    neg_score = sum(1 for w in SENTIMENT_NEGATIVE if w in text_lower)

    if pos_score > neg_score:
        return 1
    elif neg_score > pos_score:
        return -1
    return 0
