"""迁移 valuation_stock_archive.json 里非 canonical sector 到同 code 的 canonical sector。

对每条 score_history.sector 非 ecosystem canonical key（且非 unclassified）的记录，
用同 code 的 canonical sector（该 code 其它记录的 ecosystem sector）迁移。
unclassified 保留（ecosystem 外的股，无 canonical）。

用法:
  python scripts/migrate_archive_sectors.py           # dry-run，只打印
  python scripts/migrate_archive_sectors.py --apply   # 实改

场景：B2 前 stock 模式 LLM 自由链名（光通信产业链/激光产业链 等）写入的旧脏 sector，
迁移到 canonical（optical_module），让旧记录进板块召回、sector 字段统一。
未来扩展新板块时，unclassified 的股可手动改 sector 到新板块 key。

Co-Authored-By: Claude <noreply@anthropic.com>
"""
import argparse
import json
import sys

sys.path.insert(0, '/opt/stocks')
from chain_agent import config


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='实改（默认 dry-run）')
    args = ap.parse_args()

    eco = json.loads(config.ECOSYSTEM_JSON.read_text(encoding="utf-8"))
    eco_keys = set(k for k in eco if k != 'metadata')
    arc_path = config.OUTPUT_DIR / 'valuation_stock_archive.json'
    if not arc_path.exists():
        print(f'档案不存在: {arc_path}', file=sys.stderr)
        sys.exit(1)
    arc = json.loads(arc_path.read_text(encoding="utf-8"))

    migrated = 0
    unclassified = 0
    no_canon = 0
    for code, e in arc.items():
        hist = e.get('score_history') or []
        # 该 code 的 canonical sector（ecosystem key，取最新一条）
        canon_sec = next((h.get('sector') for h in hist if h.get('sector') in eco_keys), None)
        for h in hist:
            sec = h.get('sector')
            if sec == 'unclassified':
                unclassified += 1
                continue
            if sec in eco_keys:
                continue  # 已 canonical
            # 非 canonical，迁移到同 code 的 canonical
            if canon_sec:
                h['sector'] = canon_sec
                migrated += 1
                print(f'  {code} {(e.get("name") or "")[:10]:10} | {sec!r} -> {canon_sec!r} (val={h.get("val")})')
            else:
                no_canon += 1
                print(f'  ⚠️ {code} {(e.get("name") or "")[:10]:10} | {sec!r} 无同 code canonical，保留')

    print(f'\n迁移 {migrated} 条 | unclassified 保留 {unclassified} | 无 canonical 保留 {no_canon}')
    if args.apply:
        arc_path.write_text(json.dumps(arc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f'已写入 {arc_path}')
    else:
        print('（dry-run，未写盘。--apply 实改）')


if __name__ == '__main__':
    main()
