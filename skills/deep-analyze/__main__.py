"""
deep-analyze skill CLI

用法:
  # chain 模式
  python -m skills.deep-analyze --chain mlcc
  python -m skills.deep-analyze --chain 光模块 --days 14 --top-n 8 --out report.md

  # stock 模式
  python -m skills.deep-analyze --stock 300308
  python -m skills.deep-analyze --stock 中际旭创 --out verdict.md
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
        description="deep-analyze skill: 产业链深度拆解 + 投资价值判断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chain", type=str, help="产业链名（中文或英文 key，如 mlcc / 光模块）")
    group.add_argument("--stock", type=str, help="股票代码或公司名（如 300308 / 中际旭创）")
    parser.add_argument("--days", type=int, default=14, help="新闻回看窗口（默认 14 天）")
    parser.add_argument("--top-n", type=int, default=8, help="chain 模式 Top N（默认 8）")
    parser.add_argument("--out", type=str, help="输出文件路径")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    # 跑 pipeline
    if args.chain:
        result = analyzer.analyze_chain(args.chain, days=args.days, top_n=args.top_n)
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
        if args.chain:
            output = report_mod.render_chain_report(result)
        else:
            output = report_mod.render_stock_verdict(result)

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
