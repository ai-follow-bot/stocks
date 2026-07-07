"""跨 skill 共享的 per-stock 知识档案（读 + 合并写）。

档案文件 output/valuation_stock_archive.json 由各 skill 写入：
- valuation-lens 写 key_facts / evidence_pool / score_history（稀缺/前瞻/供需 维度）
- deep-analyze 写 deep_key_facts / deep_score_history（供需/国产替代/业绩兑现 维度）

两 skill 维度不同，**互不覆盖对方的 key**：写路径用 merge_upsert（只覆盖本 skill 的 key，
保留对方 key）。读路径任意 skill 都可读，get_stock_key_facts 防御性剥掉 stale
`evidence: [IDs]` 前缀（旧档案或 LLM 偶发把 evidence-id 写进 key_facts 时兜底）。
"""

import json
import re
import sys

from chain_agent import config


def archive_path():
    """per-stock 知识档案路径（所有 skill 读写）。"""
    return config.OUTPUT_DIR / "valuation_stock_archive.json"


def load_archive() -> dict:
    try:
        return json.loads(archive_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_archive(arc: dict) -> None:
    """整体写盘（调用方负责合并语义）。"""
    try:
        archive_path().write_text(
            json.dumps(arc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[knowledge.archive] 档案写盘失败: {e}", file=sys.stderr)


def get_stock_entry(code: str) -> dict:
    """返回该股的完整档案条目，无档案返回 {}。"""
    if not code:
        return {}
    return load_archive().get(code) or {}


_EVIDENCE_PREFIX_RE = re.compile(r"^\s*evidence:\s*\[[^\]]*\]\s*", re.IGNORECASE)


def strip_evidence_prefix(s: str) -> str:
    """剥掉 key_facts 开头的 stale 'evidence: [IDs]' 前缀，只留事实文本。

    LLM 的 reason 字段被要求以 'evidence: [S1,S2]' 起头；存档时该前缀是上次 run 的 ID，
    复用做 prior 时对不上本次 evidence，是噪声。valuation-lens 存档时已剥（_strip_ev），
    这里是读路径的防御性兜底（旧档案 / 手填 / LLM 偶发漏剥时统一清理）。
    """
    if not s:
        return ""
    return _EVIDENCE_PREFIX_RE.sub("", s).strip()


def get_stock_key_facts(code: str) -> dict:
    """返回该股的 key_facts（{S,F,D: reason}，已剥 stale evidence-id 前缀），无档案返回 {}。

    这是 valuation-lens 维度的历史认知（稀缺/前瞻/供需），供其它 skill（如 deep-analyze）
    作背景 prior 读取。deep-analyze 自己的维度见 get_stock_deep_facts。
    """
    kf = get_stock_entry(code).get("key_facts") or {}
    if not kf:
        return {}
    return {k: strip_evidence_prefix(v) for k, v in kf.items()}


def get_stock_deep_facts(code: str) -> dict:
    """返回该股的 deep_key_facts（{supply_demand, domestic_substitution, earnings_realization: reason}，
    已剥 stale evidence-id 前缀），无档案返回 {}。

    这是 deep-analyze 维度的历史认知（供需/国产替代/业绩兑现），供 deep-analyze 评分时
    读取自身上次结论做增量更新（与 val-lens 的 key_facts 分维度共存，互不覆盖）。
    """
    kf = get_stock_entry(code).get("deep_key_facts") or {}
    if not kf:
        return {}
    return {k: strip_evidence_prefix(v) for k, v in kf.items()}


def merge_upsert(code: str, patch: dict) -> None:
    """合并写入：把 patch 的 top-level key 合并进 entry[code]，保留其它 skill 的 key。

    patch 只应包含本 skill 负责的 key（valuation-lens 给 key_facts/evidence_pool/...，
    deep-analyze 给 deep_key_facts/deep_score_history/...）。shallow update 保证对方 key 不被覆盖。
    name/segment/sectors_seen 等共享字段由调用方自行合并后放进 patch。
    """
    if not code or not patch:
        return
    arc = load_archive()
    e = arc.get(code) or {}
    e.update(patch)
    arc[code] = e
    save_archive(arc)
