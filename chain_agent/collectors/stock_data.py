"""
Layer 3.5: 候选股深度数据 enrich（a-stock-data skill 集成）

从 ~/.claude/skills/a-stock-data/SKILL.md V3.3.0 抽取的 7 个直连 HTTP 端点：
  - 东财个股新闻 (§5.1)         eastmoney_stock_news
  - 东财研报 (§2.1)             eastmoney_research_reports
  - 同花顺热点题材归因 (§3.1)   ths_hot_stocks_with_topics
  - 龙虎榜席位 (§3.5)           dragon_tiger_list
  - 限售解禁日历 (§3.6)         lockup_release_calendar
  - 融资融券明细 (§4.1)         margin_trading
  - 个股资金流 120 日 (§4.5)    fund_flow_120d

设计原则：
  1. 所有东财请求走 em_get() 共享 session + 限流，避免被封 IP
  2. 每个函数 try/except 包裹，失败返回空，不阻塞 pipeline
  3. enrich_candidates() 入口对 candidate list 并发拉数据，写入 extras 字段
  4. 同花顺热点是板块级数据，每天拉一次，缓存到 data/ths_hot_cache.json
"""

import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date, datetime, timedelta
from pathlib import Path

import requests

from .. import config

# ===== 东财统一请求入口（节流 + 会话复用）=====
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_API = "https://reportapi.eastmoney.com/report/list"

_EM_SESSION = requests.Session()
_EM_SESSION.headers.update({"User-Agent": UA})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _em_adapter = HTTPAdapter(max_retries=Retry(
        total=3, connect=3, backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"],
    ))
    _EM_SESSION.mount("https://", _em_adapter)
    _EM_SESSION.mount("http://", _em_adapter)
except Exception:
    pass

_em_last_call = [0.0]
_em_lock = threading.Lock()


def em_get(url, params=None, headers=None, timeout=None, **kwargs):
    """东财统一请求：串行限流 + 共享 session。线程安全。"""
    if timeout is None:
        timeout = getattr(config, "EM_TIMEOUT", 15)
    min_interval = getattr(config, "EM_MIN_INTERVAL", 1.0)
    with _em_lock:
        wait = min_interval - (time.time() - _em_last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.4))
        _em_last_call[0] = time.time()
    return _EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)


def eastmoney_datacenter(report_name, columns="ALL", filter_str="", page_size=50,
                          sort_columns="", sort_types="-1"):
    """东财数据中心统一查询：龙虎榜/解禁/融资融券共用。"""
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = em_get(DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ===== 1. 东财个股新闻 (§5.1) =====

def eastmoney_stock_news(code, page_size=20):
    """东财个股新闻（JSONP 接口）。
    返回 [{title, content, time, source, url}]，失败返回 []。"""
    try:
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = json.dumps({
            "uid": "", "keyword": code,
            "type": ["cmsArticleWebOld"], "client": "web",
            "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": page_size, "preTag": "", "postTag": ""}},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": UA, "Referer": "https://so.eastmoney.com/"}
        r = em_get(url, params=params, headers=headers, timeout=15)
        text = r.text
        json_str = text[text.index("(") + 1 : text.rindex(")")]
        d = json.loads(json_str)
        rows = []
        articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
        for a in articles:
            rows.append({
                "title": re.sub(r'<[^>]+>', '', a.get("title", "")),
                "content": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                "time": a.get("date", ""),
                "source": a.get("mediaName", ""),
                "url": a.get("url", ""),
            })
        return rows
    except Exception as e:
        print(f"[stock_data] eastmoney_stock_news({code}) 失败: {e}", file=sys.stderr)
        return []


# ===== 2. 东财研报 (§2.1) =====

def eastmoney_research_reports(code, max_pages=2, max_reports=5):
    """东财个股研报列表。
    返回 [{title, publish_date, org, rating, predict_this_year_eps,
           predict_next_year_eps, info_code, industry}]，失败返回 []。"""
    try:
        all_records = []
        for page in range(1, max_pages + 1):
            params = {
                "industryCode": "*", "pageSize": "50", "industry": "*",
                "rating": "*", "ratingChange": "*",
                "beginTime": "2000-01-01", "endTime": "2030-01-01",
                "pageNo": str(page), "fields": "", "qType": "0",
                "orgCode": "", "code": code, "rcode": "",
                "p": str(page), "pageNum": str(page), "pageNumber": str(page),
            }
            r = em_get(REPORT_API, params=params,
                       headers={"Referer": "https://data.eastmoney.com/"}, timeout=30)
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            all_records.extend(rows)
            if page >= (d.get("TotalPage", 1) or 1):
                break
        out = []
        for r in all_records[:max_reports]:
            out.append({
                "title": r.get("title", ""),
                "publish_date": (r.get("publishDate") or "")[:10],
                "org": r.get("orgSName", ""),
                "rating": r.get("emRatingName", ""),
                "predict_this_year_eps": r.get("predictThisYearEps"),
                "predict_next_year_eps": r.get("predictNextYearEps"),
                "info_code": r.get("infoCode", ""),
                "industry": r.get("indvInduName", ""),
            })
        return out
    except Exception as e:
        print(f"[stock_data] eastmoney_research_reports({code}) 失败: {e}", file=sys.stderr)
        return []


# ===== 3. 同花顺热点题材归因 (§3.1) =====

_THS_CACHE_PATH = config.DATA_DIR / "ths_hot_cache.json"


def ths_hot_stocks_with_topics(force_refresh=False):
    """同花顺当日强势股 + 题材归因 reason tags。
    返回 [{code, name, reason, zhangfu, huanshou, chengjiaoe, ddejingliang}]。
    板块级数据，1 天缓存一次。"""
    # 缓存检查
    today = _date.today().strftime("%Y-%m-%d")
    if not force_refresh and _THS_CACHE_PATH.exists():
        try:
            cache = json.loads(_THS_CACHE_PATH.read_text(encoding="utf-8"))
            if cache.get("date") == today:
                return cache.get("stocks", [])
        except Exception:
            pass

    try:
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{today}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        if data.get("errocode", 0) != 0:
            raise RuntimeError(f"同花顺热点错误: {data.get('errormsg', '')}")
        rows = data.get("data") or []
        stocks = []
        for it in rows:
            stocks.append({
                "code": str(it.get("code", "")),
                "name": it.get("name", ""),
                "reason": it.get("reason", ""),
                "zhangfu": it.get("zhangfu", 0),
                "huanshou": it.get("huanshou", 0),
                "chengjiaoe": it.get("chengjiaoe", 0),
                "ddejingliang": it.get("ddejingliang", 0),
            })
        # 写缓存
        try:
            _THS_CACHE_PATH.write_text(
                json.dumps({"date": today, "stocks": stocks}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return stocks
    except Exception as e:
        print(f"[stock_data] ths_hot_stocks_with_topics 失败: {e}", file=sys.stderr)
        return []


# ===== 4. 龙虎榜席位 (§3.5) =====

def dragon_tiger_list(code, days=30):
    """个股近 N 天龙虎榜记录 + 最近一次的买卖席位 TOP5 + 机构净买。
    返回 {records, seats:{buy,sell}, institution:{buy_amt, sell_amt, net_amt}}，单位万元。
    失败返回 {}。"""
    today = _date.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        records_data = eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{start}')(TRADE_DATE<='{today}')(SECURITY_CODE=\"{code}\")",
            page_size=50, sort_columns="TRADE_DATE", sort_types="-1",
        )
        records = [{
            "date": str(r.get("TRADE_DATE", ""))[:10],
            "reason": r.get("EXPLANATION", ""),
            "net_buy": round((r.get("BILLBOARD_NET_AMT") or 0) / 10000, 1),
            "turnover": round(float(r.get("TURNOVERRATE") or 0), 2),
        } for r in records_data]

        seats = {"buy": [], "sell": []}
        institution = {"buy_amt": 0.0, "sell_amt": 0.0, "net_amt": 0.0}
        if records:
            latest = records[0]["date"]
            buy_data = eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
                page_size=10, sort_columns="BUY", sort_types="-1",
            )
            sell_data = eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")",
                page_size=10, sort_columns="SELL", sort_types="-1",
            )
            for r in buy_data[:5]:
                seats["buy"].append({
                    "name": r.get("OPERATEDEPT_NAME", ""),
                    "buy_amt": round((r.get("BUY") or 0) / 10000, 1),
                    "sell_amt": round((r.get("SELL") or 0) / 10000, 1),
                    "net": round((r.get("NET") or 0) / 10000, 1),
                })
            for r in sell_data[:5]:
                seats["sell"].append({
                    "name": r.get("OPERATEDEPT_NAME", ""),
                    "buy_amt": round((r.get("BUY") or 0) / 10000, 1),
                    "sell_amt": round((r.get("SELL") or 0) / 10000, 1),
                    "net": round((r.get("NET") or 0) / 10000, 1),
                })
            # 机构专用席位：OPERATEDEPT_CODE == "0"
            for r in buy_data:
                if str(r.get("OPERATEDEPT_CODE", "")) == "0":
                    institution["buy_amt"] += (r.get("BUY") or 0)
            for r in sell_data:
                if str(r.get("OPERATEDEPT_CODE", "")) == "0":
                    institution["sell_amt"] += (r.get("SELL") or 0)
            institution["buy_amt"] = round(institution["buy_amt"] / 10000, 1)
            institution["sell_amt"] = round(institution["sell_amt"] / 10000, 1)
            institution["net_amt"] = round(institution["buy_amt"] - institution["sell_amt"], 1)

        return {"records": records, "seats": seats, "institution": institution}
    except Exception as e:
        print(f"[stock_data] dragon_tiger_list({code}) 失败: {e}", file=sys.stderr)
        return {}


# ===== 5. 限售解禁日历 (§3.6) =====

def lockup_release_calendar(code, days_ahead=90):
    """未来 N 天解禁 + 历史解禁。
    返回 {upcoming: [{date, type, shares, ratio}], history: [...],
          days_until: int 或 None}，失败返回 {}。"""
    today = _date.today().strftime("%Y-%m-%d")
    end = (datetime.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    try:
        # 未来解禁
        up_data = eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(SECURITY_CODE=\"{code}\")(FREE_DATE>='{today}')(FREE_DATE<='{end}')",
            page_size=20, sort_columns="FREE_DATE", sort_types="1",
        )
        upcoming = [{
            "date": str(r.get("FREE_DATE", ""))[:10],
            "type": r.get("LIMITED_STOCK_TYPE", ""),
            "shares": r.get("FREE_SHARES_NUM", 0),
            "ratio": r.get("FREE_RATIO", 0),
        } for r in up_data]

        # 历史解禁（最近 10 批）
        hist_data = eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(SECURITY_CODE=\"{code}\")",
            page_size=10, sort_columns="FREE_DATE", sort_types="-1",
        )
        history = [{
            "date": str(r.get("FREE_DATE", ""))[:10],
            "type": r.get("LIMITED_STOCK_TYPE", ""),
            "shares": r.get("FREE_SHARES_NUM", 0),
            "ratio": r.get("FREE_RATIO", 0),
        } for r in hist_data]

        days_until = None
        if upcoming:
            d0 = datetime.today().date()
            d1 = datetime.strptime(upcoming[0]["date"], "%Y-%m-%d").date()
            days_until = (d1 - d0).days

        return {"upcoming": upcoming, "history": history, "days_until": days_until}
    except Exception as e:
        print(f"[stock_data] lockup_release_calendar({code}) 失败: {e}", file=sys.stderr)
        return {}


# ===== 6. 融资融券明细 (§4.1) =====

def margin_trading(code, days=30):
    """融资融券明细（日级，最近 N 天）。
    返回 {rows: [{date, rzye, rzmre, rqye, rzrqye}],
          margin_balance_change: float(近 N 天融资余额变化百分比)}，失败返回 {}。"""
    try:
        data = eastmoney_datacenter(
            "RPTA_WEB_RZRQ_GGMX",
            filter_str=f'(SCODE="{code}")',
            page_size=days, sort_columns="DATE", sort_types="-1",
        )
        rows = [{
            "date": str(r.get("DATE", ""))[:10],
            "rzye": r.get("RZYE", 0),
            "rzmre": r.get("RZMRE", 0),
            "rqye": r.get("RQYE", 0),
            "rzrqye": r.get("RZRQYE", 0),
        } for r in data]
        # 融资余额变化百分比（最近 vs 最早）
        change_pct = 0.0
        if len(rows) >= 2 and rows[-1]["rzye"]:
            latest = rows[0]["rzye"] or 0
            earliest = rows[-1]["rzye"] or 0
            if earliest:
                change_pct = round((latest - earliest) / earliest, 4)
        return {"rows": rows, "margin_balance_change": change_pct}
    except Exception as e:
        print(f"[stock_data] margin_trading({code}) 失败: {e}", file=sys.stderr)
        return {}


# ===== 7. 个股资金流 120 日 (§4.5) =====

# push2his.eastmoney.com 对部分大陆住宅 IP 有连接级风控（SKILL §4.5 已知问题），
# 失败一次后整轮 enrich 内不再重复尝试，避免日志刷屏。
_fund_flow_disabled = {"disabled": False}


def fund_flow_120d(code):
    """个股资金流日级（最近 120 个交易日）。
    返回 {rows: [{date, main_net, small_net, mid_net, large_net, super_net}],
          main_net_inflow: float(120日累计主力净流入, 元)}，失败返回 {}。"""
    if _fund_flow_disabled["disabled"]:
        return {}
    market_code = 1 if code.startswith("6") else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": f"{market_code}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": "120",
    }
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    try:
        r = em_get(url, params=params, headers=headers, timeout=15)
        d = r.json()
        klines = d.get("data", {}).get("klines", [])
        rows = []
        for line in klines:
            parts = line.split(",")
            if len(parts) >= 7:
                rows.append({
                    "date": parts[0],
                    "main_net": float(parts[1]) if parts[1] != "-" else 0,
                    "small_net": float(parts[2]) if parts[2] != "-" else 0,
                    "mid_net": float(parts[3]) if parts[3] != "-" else 0,
                    "large_net": float(parts[4]) if parts[4] != "-" else 0,
                    "super_net": float(parts[5]) if parts[5] != "-" else 0,
                })
        main_inflow = round(sum(r["main_net"] for r in rows), 0) if rows else 0
        return {"rows": rows, "main_net_inflow": main_inflow}
    except Exception as e:
        # push2his 间歇风控：禁用本端点本轮，避免 N 只候选股刷屏
        if not _fund_flow_disabled["disabled"]:
            _fund_flow_disabled["disabled"] = True
            print(f"[stock_data] fund_flow_120d 首次失败({code}): {e} — "
                  f"push2his 风控（SKILL §4.5 已知问题），本轮禁用该端点",
                  file=sys.stderr)
        return {}


# ===== 阶段入口：enrich_candidates =====

def _enrich_one(code, ths_map):
    """对单只股票拉 6 类数据（THS 热点是板块级，从 ths_map 查）。"""
    extras = {}
    # 1. 东财个股新闻（前 5 条）
    news = eastmoney_stock_news(code, page_size=5)
    if news:
        extras["stock_news"] = news
    # 2. 东财研报（前 3 条）
    rpts = eastmoney_research_reports(code, max_pages=1, max_reports=3)
    if rpts:
        extras["research_reports"] = rpts
    # 3. 龙虎榜（近 30 天）
    dt = dragon_tiger_list(code, days=30)
    if dt:
        extras["dragon_tiger"] = dt
    # 4. 解禁日历（未来 90 天）
    lu = lockup_release_calendar(code, days_ahead=90)
    if lu:
        extras["lockup"] = lu
    # 5. 融资融券（近 30 天）
    mg = margin_trading(code, days=30)
    if mg:
        extras["margin"] = mg
    # 6. 资金流 120 日
    ff = fund_flow_120d(code)
    if ff:
        extras["fund_flow_120d"] = ff
    # 7. THS 热点题材（若该股在热点榜）
    if code in ths_map:
        extras["ths_topics"] = ths_map[code]
    return extras


def enrich_candidates(candidates, sector=None, max_workers=5, limit=None):
    """对 candidate list 并发拉深度数据，写入每个 dict 的 `extras` 字段。

    Args:
        candidates: discover_candidates() 返回的 candidate list
        sector: 板块名（仅用于日志）
        max_workers: 并发数（东财限流由 em_get 保证，并发只加速非东财部分）
        limit: 只 enrich 前 N 只（None=全部；建议 ≤30 避免被风控）

    Returns:
        原 list（in-place 修改），每个 dict 增加 `extras` 字段。
        所有调用失败时 extras 为 {}，pipeline 不阻塞。
    """
    if not candidates:
        return candidates

    # 板块级：同花顺热点（1 次/天，缓存）
    print(f"[stock_data] 拉同花顺热点题材归因...", file=sys.stderr)
    ths_stocks = ths_hot_stocks_with_topics()
    ths_map = {s["code"]: s for s in ths_stocks if s.get("code")}
    print(f"[stock_data] 同花顺热点 {len(ths_map)} 只强势股", file=sys.stderr)

    # 限制 enrich 数量，避免候选池过大触发风控
    targets = candidates if (limit is None or limit >= len(candidates)) else candidates[:limit]
    print(f"[stock_data] enrich {len(targets)}/{len(candidates)} 只候选股 "
          f"(max_workers={max_workers})", file=sys.stderr)

    def _task(c):
        return c["code"], _enrich_one(str(c["code"]), ths_map)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_task, c): c for c in targets}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                code, extras = fut.result()
                c["extras"] = extras
            except Exception as e:
                print(f"[stock_data] enrich {c.get('code')} 异常: {e}", file=sys.stderr)
                c["extras"] = {}

    # 没在 targets 中的候选（被 limit 截断的）补空 extras
    for c in candidates:
        if "extras" not in c:
            c["extras"] = {}

    return candidates
