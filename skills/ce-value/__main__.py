"""ce-value skill CLI

用法:
  # 用户指定板块模式（宏观/市场作自上而下框定 + 三高 + 卡脖子）
  python -m skills.ce-value --chain 物理AI --top-n 8 --days 14 --out report.md
  python -m skills.ce-value --chain optical_module

  # LLM 自动选板块模式（宏观/市场简报 -> 选 1-N 板块再下沉）
  python -m skills.ce-value --top-n 8 --days 14 --out report.md
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
        description="ce-value: 431 中国特色价值投资（宏观->市场->行业->公司 + 三高 + 卡脖子）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--chain", type=str, default=None,
                        help="产业链名（中文或英文 key）。省略则 LLM 自动选板块")
    parser.add_argument("--days", type=int, default=14, help="新闻回看窗口（默认 14 天）")
    parser.add_argument("--top-n", type=int, default=8, help="每板块候选上限（默认 8）")
    parser.add_argument("--max-sectors", type=int, default=2,
                        help="自动选板块模式的上限（默认 2，1-3）")
    parser.add_argument("--out", type=str, help="输出文件路径")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    result = analyzer.run_ce_value(
        sector=args.chain, days=args.days, top_n=args.top_n,
        max_sectors=args.max_sectors,
    )

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
