"""cycle-lens 独立 archive（SPEC §6）：predict_EPS 累积 + 上修/下修检测。

output/cycle_archive.json: {code: {name, predict_eps_history: [{ts, predict_this, predict_next}],
last_run, runs}}
每次跑追加 predict_eps_history，对比上次 predict_this -> 上修/下修/持平/首次。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

ARCHIVE_PATH = Path("/opt/stocks/output/cycle_archive.json")


def _load() -> dict:
    try:
        if ARCHIVE_PATH.exists():
            return json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[cycle-lens] archive 读取失败: {e}", file=sys.stderr)
    return {}


def _save(arc: dict):
    try:
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVE_PATH.write_text(json.dumps(arc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[cycle-lens] archive 写入失败: {e}", file=sys.stderr)


def load_entry(code: str) -> dict:
    return _load().get(code, {})


def upsert_predict_eps(code: str, name: str, predict_this, predict_next) -> dict:
    """存本次 predict_EPS + timestamp；对比上次 -> 返回 {revision, prev, curr}。

    revision: 上修(curr>prev)/下修(curr<prev)/持平/首次(无历史)/无数据。
    """
    arc = _load()
    entry = arc.get(code, {})
    history = entry.get("predict_eps_history") or []
    prev = history[-1]["predict_this"] if history else None

    revision = "无数据"
    if predict_this is None:
        revision = "无数据"
    elif prev is None:
        revision = "首次"
    elif predict_this > prev:
        revision = "上修"
    elif predict_this < prev:
        revision = "下修"
    else:
        revision = "持平"

    if predict_this is not None:
        history.append({
            "ts": datetime.now().isoformat(),
            "predict_this": predict_this,
            "predict_next": predict_next,
        })
        # 只保留近 20 条
        history = history[-20:]

    entry["name"] = name or entry.get("name", "")
    entry["predict_eps_history"] = history
    entry["last_run"] = datetime.now().isoformat()
    entry["runs"] = entry.get("runs", 0) + 1
    arc[code] = entry
    _save(arc)

    return {"revision": revision, "prev": prev, "curr": predict_this}
