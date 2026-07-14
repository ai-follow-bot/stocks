"""运行时 prompt override（SPEC_closed_loop Part 2：prompt 类一键耦合）。

prompt_synth / prompt_conclusion / prompt_risk / search_depth 类改进建议，apply 后写
data/prompt_overrides.json（key=fingerprint `{sector}|{target}|{value}`）。各 skill 生成时
按 (sector, target) 读已 applied 的 override，append 到对应 system prompt 作「本板块额外要求」。

三条约束（用户要求）：
- 能用到：runtime 注入到 prompt，真生效到下一份报告。
- 不影响别的系统：按 sector 隔离，只本板块生成时读自己的 override；不改全局 prompt 常量。
- 重复跑不歧义：指纹去重（key 含 value），每次重读 fresh 不追加到源码；业绩更新/新股/公告
  触发的重跑读到同一份 override，一致生效，不累积重复。
"""

import json
import re
from pathlib import Path

from chain_agent import config

OVERRIDES_JSON = config.DATA_DIR / "prompt_overrides.json"

_cache = {"mtime": -1, "data": {}}


def load_prompt_overrides() -> dict:
    """读 data/prompt_overrides.json（mtime 缓存）。缺失/不可读 -> {}（graceful）。"""
    try:
        if not OVERRIDES_JSON.exists():
            return {}
        stat = OVERRIDES_JSON.stat()
        if _cache["mtime"] == stat.st_mtime:
            return _cache["data"]
        data = json.loads(OVERRIDES_JSON.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
        _cache["mtime"] = stat.st_mtime
        _cache["data"] = data
        return data
    except Exception:
        return {}


def _norm_sector(s: str) -> str:
    return (s or "").strip().lower()


def get_active_overrides(sector: str, target: str) -> list:
    """返回该 (sector, target) 下所有 applied override 的 value 列表（保持入档顺序，去重）。

    用于 harness/deep-analyze 把 override append 到 system prompt 的「本板块额外要求」段。
    target ∈ {prompt_synth, prompt_conclusion, prompt_risk}。
    """
    data = load_prompt_overrides()
    sec = _norm_sector(sector)
    seen = set()
    out = []
    for _fp, e in data.items():
        if not isinstance(e, dict):
            continue
        if _norm_sector(e.get("sector")) != sec:
            continue
        if e.get("target") != target:
            continue
        v = str(e.get("value") or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def render_override_block(sector: str, target: str, header: str = "本板块额外要求（来自改进闭环，仅本板块生效）") -> str:
    """生成 append 到 system prompt 末尾的 override 段；无 override 返回空串。

    例：
      \\n\\n【本板块额外要求（来自改进闭环，仅本板块生效）】\\n- xxx\\n- yyy
    """
    items = get_active_overrides(sector, target)
    if not items:
        return ""
    return "\n\n【" + header + "】\n" + "\n".join(f"- {it}" for it in items)


def get_search_depth(sector: str, default: int) -> int:
    """返回该 sector 的 search_depth override 数值与 default 的较大者。

    search_depth 的 value 形如「加大 tavily_results 到 15」；取其中的数字。无 override -> default。
    取 max 避免错误的小数值降低搜索深度。
    """
    items = get_active_overrides(sector, "search_depth")
    best = default
    for v in items:
        m = re.search(r"(\d+)", v)
        if m:
            n = int(m.group(1))
            if n > best:
                best = n
    return best
