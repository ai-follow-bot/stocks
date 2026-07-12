#!/usr/bin/env python3
"""构建全市场多标签板块分类知识库。

Phase A: 申万行业映射（legulegu cons，限速+断点续传）-> data/sw_industry_map.json
Phase B: LLM 多标签分类（批量+逐只缓存）    -> data/stock_classification.json

设计要点：
- 全程不碰东财 push2（本机 IP 被封）；申万 info 走 akshare，cons 走 legulegu.com（复刻 akshare
  的请求，按列位置解析，绕开 akshare 1.18.64 列名过期的 bug）。
- 限速防封：legulegu 默认 3s+抖动/次；LLM 默认 20 只/批 × 2 并发。env 可调。
- 断点续传：三级 cons 逐个落盘 output/sw_cons_cache.json；LLM 逐只落盘 output/classify_cache.json。
  任何一步挂了重跑从断点继续。

用法:
  /opt/stocks/.venv/bin/python scripts/build_stock_classification.py --phase a
  /opt/stocks/.venv/bin/python scripts/build_stock_classification.py --phase b
  /opt/stocks/.venv/bin/python scripts/build_stock_classification.py --phase all
  /opt/stocks/.venv/bin/python scripts/build_stock_classification.py --phase b --limit 50   # 试跑
"""
import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A 股链路必须直连（chain_agent.config 也会清，这里先清防 requests 走代理）
for _k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"]:
    os.environ.pop(_k, None)

import pandas as pd
import requests
from chain_agent import config

DATA = ROOT / "data"
OUTPUT = ROOT / "output"
SW_MAP_JSON = DATA / "sw_industry_map.json"
CLASS_JSON = DATA / "stock_classification.json"
SW_CACHE = OUTPUT / "sw_cons_cache.json"       # {三级code: [{code,name}]} 断点续传
CLASS_CACHE = OUTPUT / "classify_cache.json"   # {code: {sectors}} 逐只缓存

# 限速配置（env 可覆盖）
SW_MIN_INTERVAL = float(os.environ.get("SW_MIN_INTERVAL", "3.0"))
CLASSIFY_BATCH = int(os.environ.get("CLASSIFY_BATCH", "20"))
CLASSIFY_CONCURRENCY = int(os.environ.get("CLASSIFY_CONCURRENCY", "2"))
LLM_MAX_CONTINUES = int(os.environ.get("LLM_MAX_CONTINUES", "2"))

# 预筛：科技相关申万一级（覆盖 30 板块所需）
TECH_SW_L1 = os.environ.get(
    "TECH_SW_L1", "电子,计算机,通信,电力设备,机械设备,国防军工,基础化工,有色金属,汽车"
).split(",")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
LEGU_URL = "https://legulegu.com/stockdata/index-composition?industryCode={code}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ============================ Phase A: 申万行业映射 ============================
def phase_a():
    import akshare as ak

    print("[A] 拉取申万二/三级行业列表（akshare，直连）...")
    df2 = ak.sw_index_second_info()   # 二级: 行业代码/行业名称/上级行业(=一级)
    df3 = ak.sw_index_third_info()    # 三级: 行业代码/行业名称/上级行业(=二级)/成份个数
    l2_to_l1 = {r["行业名称"]: r["上级行业"] for _, r in df2.iterrows()}

    thirds = []
    for _, r in df3.iterrows():
        l3, l2 = r["行业名称"], r["上级行业"]
        l1 = l2_to_l1.get(l2, "")
        thirds.append({"code": r["行业代码"], "l3": l3, "l2": l2, "l1": l1})

    tech = [t for t in thirds if t["l1"] in TECH_SW_L1]
    print(f"[A] 三级总数 {len(thirds)} | 科技相关(一级={TECH_SW_L1}) {len(tech)} 个三级")

    cache = _load_json(SW_CACHE, {})
    todo = [t for t in tech if t["code"] not in cache]
    print(f"[A] 已缓存 {len(cache)} | 待拉 {len(todo)} | 限速 {SW_MIN_INTERVAL}s+抖动/次")

    for i, t in enumerate(todo):
        ok = False
        for attempt in range(3):  # 单三级最多重试 3 次
            try:
                r = requests.get(LEGU_URL.format(code=t["code"]), headers=UA, timeout=20)
                if r.status_code == 403:
                    back = 10 + attempt * 10
                    print(f"[A] {t['code']} 403 疑似限流，退避 {back}s", flush=True)
                    time.sleep(back); continue
                dfs = pd.read_html(StringIO(r.text))
                stocks = []
                if dfs:
                    df = dfs[0]
                    for _, row in df.iterrows():
                        code = str(row.iloc[1]).strip()
                        # legulegu 的股票代码带 .SH/.SZ 后缀，剥掉
                        if "." in code:
                            code = code.split(".")[0]
                        name = str(row.iloc[2]).strip()
                        if code.isdigit() and len(code) == 6 and name and name.lower() != "nan":
                            stocks.append({"code": code, "name": name})
                cache[t["code"]] = stocks
                ok = True
                print(f"[A] {i+1}/{len(todo)} {t['code']} {t['l3']}({t['l1']}/{t['l2']}): {len(stocks)} 只", flush=True)
                break
            except Exception as e:
                print(f"[A] {t['code']} {t['l3']} 第{attempt+1}次失败: {str(e)[:80]}", flush=True)
                time.sleep(5 + attempt * 5)
        if not ok:
            print(f"[A] {t['code']} {t['l3']} 3 次失败，跳过（重跑可重试，未入缓存）", flush=True)
            continue  # 不缓存失败，重跑会重试
        # 落盘（每 3 个一次，结尾再保一次）
        if (i + 1) % 3 == 0:
            _save_json(SW_CACHE, cache)
        time.sleep(SW_MIN_INTERVAL + random.uniform(0, 1.5))
    _save_json(SW_CACHE, cache)

    # 汇总 stock -> 申万 lineage
    sw_map = {}
    for t in tech:
        for s in cache.get(t["code"], []):
            code = s["code"]
            if code not in sw_map:
                sw_map[code] = {"name": s["name"], "sw": {"l1": t["l1"], "l2": t["l2"], "l3": t["l3"]}}
    _save_json(SW_MAP_JSON, {"update_time": _now(), "count": len(sw_map), "stocks": sw_map})
    print(f"[A] 完成: {len(sw_map)} 只科技相关股票 -> {SW_MAP_JSON}")


# ============================ Phase B: LLM 多标签分类 ============================
def _sector_catalog() -> list:
    """从 ecosystem + keywords 拼出 30 板块目录（key/name/desc/key_products）。"""
    eco = json.loads((DATA / "sector_ecosystem.json").read_text(encoding="utf-8"))
    kw = json.loads((DATA / "sector_keywords.json").read_text(encoding="utf-8"))
    sectors_kw = kw.get("sectors", {})
    cat = []
    for k, v in eco.items():
        if k == "metadata" or not isinstance(v, dict):
            continue
        cat.append({
            "key": k,
            "name": v.get("name", k),
            "desc": v.get("description", ""),
            "key_products": v.get("key_products", []) or sectors_kw.get(k, []),
        })
    return cat


def _llm_call(system: str, user: str) -> str:
    """单次 LLM 调用 + max_tokens 自动续写（复用 deep-analyze 思路，简单版）。"""
    from chain_agent.llm.client import get_llm_client
    client = get_llm_client()
    if client is None:
        raise RuntimeError("LLM 不可用")
    if not hasattr(client, "synthesize_with_meta"):
        return client.synthesize(system, user) or ""
    meta = client.synthesize_with_meta(system, user)
    text = meta.get("text") or ""
    parts = [text]
    for _ in range(LLM_MAX_CONTINUES):
        if meta.get("stop_reason") != "max_tokens":  # 未截断则无需续写
            break
        cont_meta = client.synthesize_with_meta(
            system, user + "\n\n[接上文，继续输出未完成的 JSON，不要重复已输出部分]"
        )
        cont = cont_meta.get("text") or ""
        if not cont:
            break
        parts.append(cont)
        meta = cont_meta
    return "".join(parts)


def _classify_batch(batch: list, catalog: list) -> list:
    """对一批股票调 LLM 多标签分类。返回 [{code, sectors:[{sector,segment,confidence}]}]。"""
    from chain_agent.llm.parse import json_from_llm
    valid_keys = {c["key"] for c in catalog}
    cat_text = "\n".join(
        f"- {c['key']}（{c['name']}）: {c['desc']} | 关键产品: {', '.join(c['key_products'][:6])}"
        for c in catalog
    )
    stocks_text = json.dumps(
        [{"code": s["code"], "name": s["name"], "sw": s["sw"]} for s in batch],
        ensure_ascii=False, indent=2,
    )
    system = (
        "你是 A 股板块分类助手。给定 30 个板块目录和一批股票（含申万 1/2/3 级行业归属），"
        "判断每只股票属于哪些板块。规则：\n"
        "1. 一只股票可属于 0-N 个板块（按核心产品/产线多标签）。\n"
        "2. segment 写该股在该板块的具体环节或产品（如'刻蚀设备'/'硅片'/'FPGA'）。\n"
        "3. confidence 0-1，仅归属明确的才给高分；不确定的不要硬分（可返回空 sectors）。\n"
        "4. sector 必须是目录里的 key 之一。\n"
        "5. 申万行业是重要信号但不是唯一依据：申万'半导体材料'里的设备股应归'半导体设备'，等。\n"
        "严格输出 JSON 数组: [{\"code\":\"688012\",\"sectors\":[{\"sector\":\"半导体设备\",\"segment\":\"刻蚀设备\",\"confidence\":0.9}]}]"
    )
    user = f"板块目录:\n{cat_text}\n\n股票列表:\n{stocks_text}"
    text = _llm_call(system, user)
    data = json_from_llm(text)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        secs = []
        for s in item.get("sectors", []) or []:
            if isinstance(s, dict) and s.get("sector") in valid_keys:
                secs.append({
                    "sector": s["sector"],
                    "segment": str(s.get("segment", ""))[:40],
                    "confidence": float(s.get("confidence", 0.5) or 0.5),
                })
        out.append({"code": code, "sectors": secs})
    return out


def phase_b(limit: int = 0):
    sw_map = _load_json(SW_MAP_JSON, {})
    stocks = sw_map.get("stocks", {})
    if not stocks:
        print("[B] sw_industry_map.json 为空，请先 --phase a"); return
    catalog = _sector_catalog()
    print(f"[B] 板块目录 {len(catalog)} 个 | 待分类 {len(stocks)} 只 | 批 {CLASSIFY_BATCH}/并发 {CLASSIFY_CONCURRENCY}")

    cache = _load_json(CLASS_CACHE, {})   # {code: {sectors:[...]}}
    todo = [{"code": k, **v} for k, v in stocks.items() if k not in cache]
    if limit:
        todo = todo[:limit]
    print(f"[B] 已缓存 {len(cache)} | 待跑 {len(todo)}")
    if not todo:
        print("[B] 全部已缓存，直接汇总")
    else:
        batches = [todo[i:i + CLASSIFY_BATCH] for i in range(0, len(todo), CLASSIFY_BATCH)]
        lock = Lock()

        def _run(batch):
            out = _classify_batch(batch, catalog)
            by_code = {x["code"]: x["sectors"] for x in out}
            miss = []
            for s in batch:
                secs = by_code.get(s["code"])
                if secs is None:
                    miss.append(s)
                else:
                    with lock:
                        cache[s["code"]] = {"sectors": secs, "classified_at": _now()}
            # 批内缺失的逐只重试
            for s in miss:
                try:
                    single = _classify_batch([s], catalog)
                    secs = single[0]["sectors"] if single else []
                except Exception:
                    secs = []
                with lock:
                    cache[s["code"]] = {"sectors": secs, "classified_at": _now()}
            return len(out), len(miss)

        done = 0
        with ThreadPoolExecutor(max_workers=CLASSIFY_CONCURRENCY) as ex:
            futs = {ex.submit(_run, b): b for b in batches}
            for fut in as_completed(futs):
                n_ok, n_miss = fut.result()
                done += 1
                with lock:
                    _save_json(CLASS_CACHE, cache)
                print(f"[B] {done}/{len(batches)} 批完成 (ok={n_ok} 缺失逐只补={n_miss}, 缓存 {len(cache)})", flush=True)
                time.sleep(0.5)

    # 汇总
    result = {}
    classified = 0
    multi = 0
    for code, v in stocks.items():
        secs = cache.get(code, {}).get("sectors", [])
        result[code] = {"name": v["name"], "sw": v["sw"], "sectors": secs, "classified_at": cache.get(code, {}).get("classified_at")}
        if secs:
            classified += 1
            if len(secs) > 1:
                multi += 1
    _save_json(CLASS_JSON, {
        "metadata": {"update_time": _now(), "total": len(result), "classified": classified, "multi_label": multi},
        "stocks": result,
    })
    print(f"[B] 完成: {len(result)} 只 | 已分类 {classified} | 多标签 {multi} -> {CLASS_JSON}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["a", "b", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0, help="Phase B 试跑：只分类前 N 只")
    args = ap.parse_args()
    if args.phase in ("a", "all"):
        phase_a()
    if args.phase in ("b", "all"):
        phase_b(limit=args.limit)


if __name__ == "__main__":
    main()
