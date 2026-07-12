"""cycle-lens CLI（SPEC §10）。

用法:
  python -m skills.cycle-lens --stock 300308 --out cycle.md
  python -m skills.cycle-lens --stock 中际旭创 --json
  python -m skills.cycle-lens --chain HBM --out cycle.md
"""

import argparse
import json
import sys

from . import analyzer
from . import report


def main():
    ap = argparse.ArgumentParser(prog="skills.cycle-lens", description="业绩-估值周期镜头")
    ap.add_argument("--stock", help="单股：6位代码或公司名")
    ap.add_argument("--chain", help="板块：跑共享数据层候选池")
    ap.add_argument("--days", type=int, default=14, help="新闻回看天数（默认14）")
    ap.add_argument("--top-n", type=int, default=15, help="板块模式候选上限（默认15）")
    ap.add_argument("--out", help="输出 markdown 文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    args = ap.parse_args()

    if not args.stock and not args.chain:
        ap.error("必须指定 --stock 或 --chain")
    if args.stock and args.chain:
        ap.error("--stock 与 --chain 互斥")

    result = analyzer.run_cycle_lens(
        stock=args.stock, sector=args.chain, days=args.days, top_n=args.top_n,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        md = report.render_report(result)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md)
            print(f"[cycle-lens] 报告已写入 {args.out}", file=sys.stderr)
        else:
            print(md)

    if result.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
