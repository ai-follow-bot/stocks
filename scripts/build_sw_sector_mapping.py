#!/usr/bin/env python3
"""派生「申万三级 -> 前端30板块」映射表。

从 data/stock_classification.json 聚合：对每个申万三级，收集其下所有股票被 LLM 分到
的前端板块（去重 + 计数），作为可审计可编辑的对照参考表。

产出 data/sw_sector_mapping.json：
  { "<申万三级>": {"sectors": [...], "counts": {sector: n}, "stock_count": N,
                   "classified_count": M, "notes": "", "auto": true}, ... }

只覆盖 data/sw_industry_map.json 里预筛的科技三级（~133 个）。无映射的三级 sectors:[]。
auto:true 标记自动派生；用户前端编辑后写回时保留该字段供后续判断。

用法: /opt/stocks/.venv/bin/python scripts/build_sw_sector_mapping.py
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SW_MAP_JSON = DATA / "sw_industry_map.json"
CLASS_JSON = DATA / "stock_classification.json"
OUT_JSON = DATA / "sw_sector_mapping.json"


def main():
    sw = json.loads(SW_MAP_JSON.read_text(encoding="utf-8"))["stocks"]
    cl = json.loads(CLASS_JSON.read_text(encoding="utf-8"))["stocks"]

    # 每个申万三级 -> 股票列表（来自 sw_industry_map，含未分类的）+ lineage
    sw3_stocks = defaultdict(list)
    sw3_lineage = {}  # sw3 -> (l1, l2)
    for code, v in sw.items():
        sw3 = v["sw"]["l3"]
        sw3_stocks[sw3].append(code)
        if sw3 not in sw3_lineage:
            sw3_lineage[sw3] = (v["sw"].get("l1", ""), v["sw"].get("l2", ""))

    # 每个申万三级 -> 前端板块计数（来自 stock_classification 的 LLM 结果）
    sw3_sector_counts = defaultdict(Counter)
    for code, rec in cl.items():
        sw3 = rec.get("sw", {}).get("l3")
        if not sw3:
            continue
        for s in rec.get("sectors", []) or []:
            if isinstance(s, dict) and s.get("sector"):
                sw3_sector_counts[sw3][s["sector"]] += 1

    # 合并产出
    out = {}
    for sw3, codes in sw3_stocks.items():
        counts = sw3_sector_counts.get(sw3, Counter())
        sectors = [s for s, _ in counts.most_common()]  # 按频次降序
        l1, l2 = sw3_lineage.get(sw3, ("", ""))
        out[sw3] = {
            "sectors": sectors,
            "counts": dict(counts),
            "l1": l1,
            "l2": l2,
            "stock_count": len(codes),
            "classified_count": sum(counts.values()),
            "notes": "",
            "auto": True,
        }

    # 保留用户已编辑的 notes / auto=False（重跑不覆盖手动改动）
    if OUT_JSON.exists():
        old = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        for sw3, rec in out.items():
            if sw3 in old:
                old_rec = old[sw3]
                if old_rec.get("notes"):
                    rec["notes"] = old_rec["notes"]
                if old_rec.get("auto") is False:
                    rec["auto"] = False  # 用户手动改过的，标 auto=False

    OUT_JSON.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    mapped = sum(1 for r in out.values() if r["sectors"])
    print(f"派生 {len(out)} 个申万三级 -> {OUT_JSON}")
    print(f"  有映射(sectors非空): {mapped} | 无映射: {len(out) - mapped}")
    print(f"  示例:")
    for sw3 in ["通信网络设备及器件", "半导体设备", "数字芯片设计", "军工电子Ⅲ", "纯碱"]:
        if sw3 in out:
            r = out[sw3]
            print(f"    {sw3}: {r['sectors']} (股票{r['stock_count']}/分类{r['classified_count']})")


if __name__ == "__main__":
    main()
