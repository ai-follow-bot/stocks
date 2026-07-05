#!/usr/bin/env python3
"""
刷新 A 股全名单 (data/a_stock_list.json)

使用 akshare.stock_info_a_code_name() 拉取沪深北全部 A 股，
保存为 {stocks: {code: {name, ...}}} 格式，供 StockDetector 使用。

用法:
  python scripts/refresh_stock_list.py
"""

import json
import sys
from pathlib import Path

# 让脚本独立于 venv 也可运行（用系统 python + 项目 venv 都行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chain_agent import config


def main():
    try:
        import akshare as ak
    except ImportError:
        print("❌ akshare 未安装，请先: pip install akshare", file=sys.stderr)
        sys.exit(1)

    print("📊 拉取 A 股全名单 (沪深北)...")
    try:
        df = ak.stock_info_a_code_name()
    except Exception as e:
        print(f"❌ 拉取失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 字段: code, name
    stocks = {}
    for _, row in df.iterrows():
        code = str(row.get("code", "")).zfill(6)
        name = str(row.get("name", "")).strip()
        if code and name:
            stocks[code] = {"name": name}

    out = {
        "version": "1.0",
        "update_time": __import__("datetime").datetime.now().isoformat(),
        "total_count": len(stocks),
        "stocks": stocks,
    }

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_file = config.STOCK_LIST_JSON
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"✅ 已保存: {out_file}")
    print(f"   总数: {len(stocks)} 只 A 股")
    print(f"   沪市 6 开头: {sum(1 for c in stocks if c.startswith('6'))} 只")
    print(f"   深市 0/3 开头: {sum(1 for c in stocks if c[0] in '03')} 只")
    print(f"   北交所 8/4 开头: {sum(1 for c in stocks if c[0] in '84')} 只")


if __name__ == "__main__":
    main()
