"""harness skill CLI

用法:
  # chain 模式：三路径（chain_agent + deep-analyze + valuation-lens）交叉验证
  python -m skills.harness --chain optical-module --top-n 6 --out report.md
  python -m skills.harness --chain 光模块 --days 14 --top-n 8

  # stock 模式：两路径（deep-analyze + valuation-lens）交叉（chain_agent 无单股）
  python -m skills.harness --stock 300308 --out verdict.md
"""

import argparse
import json
import sys
from pathlib import Path

from chain_agent import config

from . import orchestrator
from . import report as report_mod


def main():
    parser = argparse.ArgumentParser(
        description="harness skill: 三视角交叉验证（chain_agent + deep-analyze + valuation-lens）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chain", type=str, help="产业链名（三路径交叉）")
    group.add_argument("--stock", type=str, help="单只股票代码或公司名（deep+val 两路径交叉）")
    parser.add_argument("--days", type=int, default=14, help="新闻回看窗口（默认 14 天）")
    parser.add_argument("--top-n", type=int, default=8, help="chain 模式候选上限（默认 8）")
    parser.add_argument("--out", type=str, help="输出文件路径")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    if args.chain:
        result = orchestrator.run_harness_chain(args.chain, days=args.days, top_n=args.top_n)
    else:
        result = orchestrator.run_harness_stock(args.stock, days=args.days)

    if "error" in result:
        print(f"❌ 失败: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        output = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        output = report_mod.render_report(result)

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
