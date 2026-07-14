"""
valuation-lens skill CLI

用法:
  # chain 模式：自动发现候选（板块搜索 + StockDetector + 财联社热度 + 档案召回），
  # 不读 sector_overflow_config.json、无需手填 leaders；做稀缺/前瞻/供需估值排序
  python -m skills.valuation-lens --chain optical-module
  python -m skills.valuation-lens --chain 光模块 --days 14 --top-n 8 --out report.md

  # codes 模式：显式代码列表
  python -m skills.valuation-lens --codes 300308,300502,688498 --out lens.md

  # stock 模式：单股估值判断（仅 CLI；网站 valuation 任务只接 --chain，单股走 deep-analyze）
  python -m skills.valuation-lens --stock 300308
  python -m skills.valuation-lens --stock 中际旭创 --out verdict.md
"""

import argparse
import json
import sys
from pathlib import Path

from chain_agent import config

from . import analyzer
from . import report as report_mod


def main():
    parser = argparse.ArgumentParser(
        description="valuation-lens skill: 稀缺+前瞻+供需 估值镜头（PE 仅作辅助确认）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chain", type=str,
                       help="产业链名（sector_ecosystem.json 的 key 或中文名，连字符/下划线均可，如 optical-module / 光模块；候选自动发现，不读 overflow_config、无需手填 leaders）")
    group.add_argument("--codes", type=str, help="显式股票代码列表，逗号分隔（如 300308,300502）")
    group.add_argument("--stock", type=str, help="单只股票代码或公司名（如 300308 / 中际旭创）")
    parser.add_argument("--days", type=int, default=14, help="新闻回看窗口（默认 14 天）")
    parser.add_argument("--top-n", type=int, default=8, help="chain/codes 模式候选上限（默认 8）")
    parser.add_argument("--out", type=str, help="输出文件路径")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    # 跑 pipeline
    if args.chain:
        result = analyzer.analyze_chain(args.chain, days=args.days, top_n=args.top_n)
    elif args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        result = analyzer.analyze_codes(codes, days=args.days, top_n=args.top_n)
    else:
        result = analyzer.analyze_stock(args.stock, days=args.days)

    if "error" in result:
        print(f"❌ 失败: {result['error']}", file=sys.stderr)
        if result.get("raw_llm"):
            print(f"LLM 原文: {result['raw_llm']}", file=sys.stderr)
        sys.exit(1)

    # 输出
    if args.json:
        output = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        if result.get("mode") == "stock":
            output = report_mod.render_stock_verdict(result)
        else:
            output = report_mod.render_chain_report(result)

    print(output)

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = config.OUTPUT_DIR / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"\n[已保存到 {out_path}]", file=sys.stderr)


if __name__ == "__main__":
    main()
