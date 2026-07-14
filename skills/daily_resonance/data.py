"""
数据加载模块：读取财联社新闻、板块生态系统、关键词、A股名单等
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import (
    DATA_DIR,
    HERMES_NEWS_DIR,
    LATEST_NEWS_PATH,
)


def load_json(path: Path, description: str = ""):
    """安全加载 JSON 文件，出错时打印友好错误"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[data] ❌ 文件未找到: {path}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"[data] ❌ JSON 解析失败 ({description}): {e}", file=sys.stderr)
        return None


# ── 数据加载函数（惰性缓存） ──────────────────────────

_ecosystem = None
_keywords = None
_stock_list = None


def load_ecosystem() -> dict:
    """加载板块生态系统（30个板块，含上下游关系）"""
    global _ecosystem
    if _ecosystem is not None:
        return _ecosystem
    data = load_json(DATA_DIR / "sector_ecosystem.json", "sector_ecosystem")
    _ecosystem = {k: v for k, v in data.items() if k != "metadata"} if data else {}
    return _ecosystem


def load_keywords() -> dict:
    """加载板块关键词映射 {sector_key: [keyword, ...]}"""
    global _keywords
    if _keywords is not None:
        return _keywords
    data = load_json(DATA_DIR / "sector_keywords.json", "sector_keywords")
    _keywords = data.get("sectors", {}) if data else {}
    return _keywords


def load_stock_list() -> dict:
    """加载A股全名单 {code: name}"""
    global _stock_list
    if _stock_list is not None:
        return _stock_list
    data = load_json(DATA_DIR / "a_stock_list.json", "a_stock_list")
    _stock_list = data.get("stocks", {}) if data else {}
    return _stock_list


def get_sector_name(sector_key: str, ecosystem: dict) -> str:
    """获取板块中文名称"""
    sector = ecosystem.get(sector_key, {})
    return sector.get("name", sector_key)


# ── 新闻加载 ──────────────────────────────────────────

def load_news_for_date(date_str: str) -> list[dict]:
    """
    加载指定日期的财联社新闻。

    查找顺序：
    1. {HERMES_NEWS_DIR}/{date_str}/news_{date_str}.json  (每日归档)
    2. {HERMES_NEWS_DIR}/latest_news.json (7天滚动窗口，按日期过滤)
    """
    # 优先：每日归档文件
    daily_dir = HERMES_NEWS_DIR / date_str
    daily_file = daily_dir / f"news_{date_str}.json"
    if daily_file.exists():
        data = load_json(daily_file, f"news_{date_str}")
        if data and "news" in data:
            news_list = data["news"]
            print(f"[data] 从每日归档加载 {len(news_list)} 条新闻 ({date_str})",
                  file=sys.stderr)
            return news_list

    # 回退：latest_news.json 按日期过滤
    if LATEST_NEWS_PATH.exists():
        data = load_json(LATEST_NEWS_PATH, "latest_news")
        if data and "news" in data:
            news_list = [
                n for n in data["news"]
                if n.get("date") == date_str
            ]
            print(f"[data] 从 latest_news 过滤出 {len(news_list)} 条新闻 ({date_str})",
                  file=sys.stderr)
            return news_list

    print(f"[data] ⚠️ 未找到 {date_str} 的新闻数据", file=sys.stderr)
    return []


def load_today_news() -> list[dict]:
    """加载今日（运行当天）新闻 - 用于Agent 1"""
    return load_news_for_date(datetime.now().strftime("%Y-%m-%d"))


def load_yesterday_news() -> list[dict]:
    """加载昨日新闻 - 早上8点运行时应使用昨日数据"""
    yesterday = datetime.now() - timedelta(days=1)
    return load_news_for_date(yesterday.strftime("%Y-%m-%d"))


def get_date_for_run(target_date: str = None) -> str:
    """
    获取本次运行的目标日期。
    早上8点运行，应处理昨天（T-1）的新闻。
    如果指定了 --date 参数则使用指定日期。
    """
    if target_date:
        return target_date
    yesterday = datetime.now() - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


# ── 辅助函数 ──────────────────────────────────────────

def get_upstream_downstream(sector_key: str, ecosystem: dict) -> tuple:
    """获取板块的上下游关系"""
    sector = ecosystem.get(sector_key, {})
    return (
        sector.get("upstream", []),
        sector.get("downstream", []),
        sector.get("related", []),
    )
