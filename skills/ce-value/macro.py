"""431 宏观层：财联社政策/宏观新闻 + web 搜索（全球/流动性）+ akshare 宏观序列 -> LLM 简报。

全球环境 / 政策 / 流动性 三姿态。akshare 宏观序列可能拉空/陈旧，best-effort，缺失走\"未知\"。
"""

import json
import sys
from datetime import datetime
from typing import List

import akshare as ak

from chain_agent import config
from . import common, prompts


# 财联社宏观/政策关键词（命中即视为宏观相关）
_MACRO_KW = [
    "央行", "利率", "MLF", "LPR", "降准", "降息", "社融", "M2", "货币", "流动性",
    "财政", "国债", "专项债", "PMI", "CPI", "PPI", "通胀", "通缩",
    "关税", "美联储", "美债", "美元", "汇率", "人民币", "外需", "出口",
    "国务院", "会议", "政策", "规划", "补贴", "减税", "改革", "战略",
]


def _fetch_hermes_macro(limit: int = 30) -> List[dict]:
    """从财联社 latest_news.json 过滤宏观/政策相关条目，取近 limit 条。"""
    try:
        data = json.loads(config.HERMES_NEWS_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ce-value] 财联社读取失败: {e}", file=sys.stderr)
        return []
    news = data.get("news", []) or []
    matched = []
    for item in news:
        title = (item.get("title") or "") + " " + (item.get("brief") or "")
        if any(kw in title for kw in _MACRO_KW):
            matched.append({"title": item.get("title", ""), "brief": (item.get("brief") or "")[:120]})
        if len(matched) >= limit:
            break
    return matched


def _fetch_akshare_macro() -> str:
    """best-effort 拉几组宏观序列最近值，拼成文本。失败项跳过。"""
    lines = []
    fetchers = [
        ("CPI月率", lambda: ak.macro_china_cpi_monthly()),
        ("PPI", lambda: ak.macro_china_ppi()),
        ("PMI", lambda: ak.macro_china_pmi()),
        ("M2", lambda: ak.macro_china_m2_yearly()),
        ("社融", lambda: ak.macro_china_shrzgm()),
        ("社会消费品零售", lambda: ak.macro_china_consumer_goods_retail()),
        ("央行资产负债表", lambda: ak.macro_china_central_bank_balance()),
        ("商品价格指数", lambda: ak.macro_china_commodity_price_index()),
    ]
    for name, fn in fetchers:
        try:
            df = fn()
            if df is None or getattr(df, "empty", True):
                continue
            tail = df.tail(1).iloc[0]
            # 取数值列最近值（跳过非数值/商品名列）
            vals = []
            for col in df.columns:
                v = tail.get(col)
                try:
                    fv = float(v)
                    vals.append(f"{col}={fv}")
                except (ValueError, TypeError):
                    continue
            if vals:
                lines.append(f"- {name}: {', '.join(vals[:4])}")
        except Exception as e:
            print(f"[ce-value] 宏观序列 {name} 拉取失败: {str(e)[:80]}", file=sys.stderr)
    return "\n".join(lines) if lines else "(宏观序列拉取失败或为空)"


def run_macro_briefing() -> dict:
    """返回 {global, policy, liquidity, favor_sectors, summary, data_quality, raw_inputs}。"""
    print("[ce-value] === 宏观层 ===", file=sys.stderr)
    hermes = _fetch_hermes_macro(limit=30)
    hermes_text = "\n".join(f"- {h['title']}" for h in hermes) or "(财联社无宏观相关条目)"
    print(f"[ce-value] 财联社宏观新闻 {len(hermes)} 条", file=sys.stderr)

    queries = [
        "美联储 利率 2026 全球流动性 A股影响",
        "中国 央行 降准 降息 MLF 2026 流动性",
        "2026 中国 财政政策 专项债 经济",
    ]
    web = "\n\n".join(f"### {q}\n{common.web_search(q)}" for q in queries)
    ak_macro = _fetch_akshare_macro()

    user = prompts.MACRO_USER_TEMPLATE.format(
        today=datetime.now().strftime("%Y-%m-%d"),
        n=len(hermes),
        hermes_news=hermes_text[:4000],
        web_search=web[:4000],
        akshare_macro=ak_macro[:1500],
    )
    data = common._llm_call_json(prompts.MACRO_SYSTEM, user) or {}
    if not data:
        print("[ce-value] 宏观简报 LLM 失败，降级空", file=sys.stderr)
    data_quality = "ok" if data else "degraded"
    data["data_quality"] = data_quality
    return data


if __name__ == "__main__":
    print(json.dumps(run_macro_briefing(), ensure_ascii=False, indent=2))
