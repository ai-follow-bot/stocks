"""
Layer 2: akshare 新闻采集（替代 cailianshe）

完全独立，无 cron 依赖，无 JS 渲染。
- 用 akshare.stock_news_em(symbol) 拉龙头股的个股新闻
- 用 akshare.news_economic_baidu(symbol="全部") 拉宏观新闻做板块关键词过滤
"""

from datetime import datetime, timedelta
from typing import Dict, List

from .. import config
from ..discovery.stock_detector import SECTOR_KEYWORDS


def _safe_akshare_call(func, *args, **kwargs):
    """包装 akshare 调用，统一异常处理"""
    try:
        import akshare as ak
    except ImportError as e:
        return None, f"akshare 未安装: {e}"
    try:
        method = getattr(ak, func)
        return method(*args, **kwargs), None
    except Exception as e:
        return None, str(e)


def collect_demand_side(sector: str, days: int = 7, leader_codes: List[str] = None) -> Dict:
    """
    用 akshare 拉板块相关新闻。

    Args:
        sector: 板块代码
        days: 回看天数
        leader_codes: 龙头股代码列表，用于 stock_news_em 拉个股新闻。
                      None 则只做宏观新闻关键词过滤。

    Returns:
        {source, sector, news_count, news, content_text, error}
    """
    sector = config.to_under(sector)
    keywords = SECTOR_KEYWORDS.get(sector, [sector])
    cutoff = datetime.now() - timedelta(days=days)

    all_news = []

    # 1. 龙头股个股新闻
    if leader_codes:
        for code in leader_codes[:6]:  # 最多 6 只，避免请求过多
            df, err = _safe_akshare_call("stock_news_em", symbol=code)
            if err or df is None or len(df) == 0:
                continue
            # 字段：关键词, 新闻标题, 新闻内容, 发布时间, 文章来源, 新闻链接
            for _, row in df.iterrows():
                pub_str = str(row.get("发布时间", ""))
                try:
                    pub_time = datetime.strptime(pub_str[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    try:
                        pub_time = datetime.strptime(pub_str[:10], "%Y-%m-%d")
                    except Exception:
                        pub_time = datetime.now()
                if pub_time < cutoff:
                    continue
                title = str(row.get("新闻标题", ""))
                content = str(row.get("新闻内容", ""))[:500]
                all_news.append({
                    "title": title,
                    "content": content,
                    "publish_time": pub_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": f"akshare_stock_news:{code}",
                    "url": str(row.get("新闻链接", "")),
                })

    # 2. 宏观新闻做板块关键词过滤（补充非个股层面的板块动态）
    # news_economic_baidu(date="YYYYMMDD") 拉指定日期的百度财经新闻
    macro_news = []
    for d_offset in range(days):
        date_str = (datetime.now() - timedelta(days=d_offset)).strftime("%Y%m%d")
        df, err = _safe_akshare_call("news_economic_baidu", date=date_str)
        if err or df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            title = str(row.get("title", row.get("新闻标题", "")))
            content = str(row.get("content", row.get("新闻内容", "")))[:500]
            text = title + content
            if any(kw in text for kw in keywords):
                pub_str = str(row.get("date", row.get("发布时间", date_str)))
                try:
                    pub_time = datetime.strptime(pub_str[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    pub_time = datetime.strptime(date_str, "%Y%m%d")
                if pub_time < cutoff:
                    continue
                macro_news.append({
                    "title": title,
                    "content": content,
                    "publish_time": pub_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "akshare_economic_baidu",
                    "url": "",
                })
        if d_offset >= 2:  # 最多扫 3 天，避免请求过多
            break
    all_news.extend(macro_news)

    # 去重 + 排序
    seen = set()
    deduped = []
    for n in all_news:
        key = n["title"][:50]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    deduped.sort(key=lambda x: x["publish_time"], reverse=True)
    deduped = deduped[:100]

    chunks = [
        f"[{n['publish_time']}] {n['title']} | {n['content']}"
        for n in deduped[:50]
    ]

    return {
        "source": "akshare",
        "sector": sector,
        "news_count": len(deduped),
        "news": deduped,
        "content_text": "\n".join(chunks),
        "error": None if deduped else (err or "no news matched"),
    }


def collect_stock_news(leader_codes: List[str], days: int = 7) -> Dict:
    """只拉龙头股个股新闻，跳过宏观新闻板块关键词过滤。

    用于 deep-analyze 的 _segment_search：环节名（如 "陶瓷粉体"）不是
    sector_ecosystem.json 的板块代码，传给 collect_demand_side 会让
    SECTOR_KEYWORDS 查不到关键词、宏观新闻过滤为空，命中率极低。
    本函数只跑 stock_news_em，按 leader_codes 拉个股新闻。

    Returns: 结构同 collect_demand_side，sector 字段固定为 "stock_news"
    """
    cutoff = datetime.now() - timedelta(days=days)
    all_news = []
    err = None

    for code in (leader_codes or [])[:6]:
        df, err = _safe_akshare_call("stock_news_em", symbol=code)
        if err or df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            pub_str = str(row.get("发布时间", ""))
            try:
                pub_time = datetime.strptime(pub_str[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    pub_time = datetime.strptime(pub_str[:10], "%Y-%m-%d")
                except Exception:
                    pub_time = datetime.now()
            if pub_time < cutoff:
                continue
            title = str(row.get("新闻标题", ""))
            content = str(row.get("新闻内容", ""))[:500]
            all_news.append({
                "title": title,
                "content": content,
                "publish_time": pub_time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": f"akshare_stock_news:{code}",
                "url": str(row.get("新闻链接", "")),
            })

    seen = set()
    deduped = []
    for n in all_news:
        key = n["title"][:50]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    deduped.sort(key=lambda x: x["publish_time"], reverse=True)
    deduped = deduped[:100]

    chunks = [
        f"[{n['publish_time']}] {n['title']} | {n['content']}"
        for n in deduped[:50]
    ]

    return {
        "source": "akshare",
        "sector": "stock_news",
        "news_count": len(deduped),
        "news": deduped,
        "content_text": "\n".join(chunks),
        "error": None if deduped else (err or "no news matched"),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--stock":
        # 个股新闻模式：python news_akshare.py --stock 300308 300502
        codes = sys.argv[2:] or ["300308"]
        r = collect_stock_news(codes, days=7)
        print(f"akshare 个股新闻: {r['news_count']} 条, error: {r.get('error')}")
        for n in r["news"][:5]:
            print(f"  - [{n['publish_time']}] {n['title'][:60]}")
    else:
        sec = sys.argv[1] if len(sys.argv) > 1 else "optical_module"
        r = collect_demand_side(sec, days=7, leader_codes=["300308", "300502"])
        print(f"akshare 新闻: {r['news_count']} 条, error: {r.get('error')}")
        for n in r["news"][:5]:
            print(f"  - [{n['publish_time']}] {n['title'][:60]}")
