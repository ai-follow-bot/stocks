"""
Layer 2: 财联社新闻采集（复用 hermes 采集结果）

不自己跑财联社爬虫，直接读 hermes 的 latest_news.json（它有 cron 实时更新）。
按板块关键词 + 龙头股 stock_codes 双重过滤。

hermes 不在或文件过期时返回 error，调用方（orchestrator）降级到 akshare-only。
"""

from datetime import datetime, timedelta
from typing import Dict, List

from .. import config
from ..discovery.stock_detector import SECTOR_KEYWORDS


def _parse_pub_time(pub_str: str) -> datetime:
    """财联社 publish_time 是 ISO8601 (Z 后缀)，转 datetime"""
    try:
        s = pub_str.replace("Z", "+00:00")[:25]
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None)
    except Exception:
        # 退回到 date 字段
        try:
            return datetime.strptime(str(pub_str)[:10], "%Y-%m-%d")
        except Exception:
            return datetime.now()


def collect_demand_side(sector: str, days: int = 7, leader_codes: List[str] = None) -> Dict:
    """
    读 hermes latest_news.json 做板块过滤。

    Args:
        sector: 板块代码
        days: 回看天数
        leader_codes: 龙头股代码列表，匹配新闻的 stock_codes 字段

    Returns:
        {source, sector, news_count, news, content_text, error}
    """
    sector = config.to_under(sector)
    keywords = SECTOR_KEYWORDS.get(sector, [sector])
    cutoff = datetime.now() - timedelta(days=days)
    leader_set = set(leader_codes or [])

    news_path = config.HERMES_NEWS_JSON
    if not news_path.exists():
        return {
            "source": "cailianshe",
            "sector": sector,
            "news_count": 0,
            "news": [],
            "content_text": "",
            "error": f"hermes news file not found: {news_path}",
        }

    try:
        import json
        with open(news_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {
            "source": "cailianshe", "sector": sector, "news_count": 0,
            "news": [], "content_text": "", "error": f"read failed: {e}",
        }

    all_news = data.get("news", []) if isinstance(data, dict) else []

    # 新鲜度检查（hermes 自己更新失败时不要把陈旧数据当实时数据用）
    update_time_str = data.get("update_time", "") if isinstance(data, dict) else ""
    try:
        update_time = datetime.fromisoformat(update_time_str.replace("Z", "+00:00")[:25])
        update_time = update_time.replace(tzinfo=None)
        if (datetime.now() - update_time) > timedelta(hours=12):
            return {
                "source": "cailianshe", "sector": sector, "news_count": 0,
                "news": [], "content_text": "",
                "error": f"hermes news stale (update_time={update_time_str})",
            }
    except Exception:
        pass  # 解析失败不阻断，让数据自己用

    deduped = []
    seen = set()
    for n in all_news:
        pub_time = _parse_pub_time(n.get("publish_time", ""))
        if pub_time < cutoff:
            continue

        title = str(n.get("title", ""))
        content = str(n.get("content", ""))[:500]
        text = title + content

        # 命中条件：板块关键词 OR 龙头股 stock_codes
        kw_hit = any(kw in text for kw in keywords)
        news_codes = [str(c).zfill(6) for c in n.get("stock_codes", []) if c]
        code_hit = bool(leader_set & set(news_codes))
        if not (kw_hit or code_hit):
            continue

        key = title[:50]
        if key in seen:
            continue
        seen.add(key)

        deduped.append({
            "title": title,
            "content": content,
            "publish_time": pub_time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "cailianshe",
            "url": str(n.get("url", "")),
            "matched_by": "keyword" if kw_hit else "stock_code",
            "stock_codes": news_codes,
            "level": n.get("level", ""),
            "importance": n.get("importance", 0),
        })

    deduped.sort(key=lambda x: x["publish_time"], reverse=True)
    deduped = deduped[:100]

    chunks = [
        f"[{n['publish_time']}][{n.get('level','')}] {n['title']} | {n['content']}"
        for n in deduped[:50]
    ]

    return {
        "source": "cailianshe",
        "sector": sector,
        "news_count": len(deduped),
        "news": deduped,
        "content_text": "\n".join(chunks),
        "error": None if deduped else "no news matched",
    }


if __name__ == "__main__":
    import sys
    sec = sys.argv[1] if len(sys.argv) > 1 else "optical_module"
    r = collect_demand_side(sec, days=7, leader_codes=["300308", "300502"])
    print(f"财联社新闻: {r['news_count']} 条, error: {r.get('error')}")
    for n in r["news"][:5]:
        print(f"  - [{n['publish_time']}][{n.get('level','')}] {n['title'][:60]} (by {n['matched_by']})")
