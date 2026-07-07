"""valuation-lens 知识档案：per-stock + 板块级积累。

per-stock 档案（output/valuation_stock_archive.json）存 S/F/D 维度的
key_facts / evidence_pool / score_history；与 deep-analyze 的 deep_* 维度
分键共存（_upsert_archive 保留 deep_*，跨 skill 积累互通）。
板块级档案（output/valuation_sector_archive.json）存供需概要，下次注入作板块历史认知。

24h 内跑过的标的跳过 Tavily（复用档案 evidence_pool），财联社始终实时拉（per-stock）。
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from chain_agent import config
from chain_agent.collectors.snippet import snippet as _snippet
from chain_agent.knowledge.archive import (
    archive_path as _archive_path,
    load_archive as _load_archive,
    strip_evidence_prefix as _strip_ev,
)


# _archive_path / _load_archive 复用共享 chain_agent/knowledge/archive.py（供跨 skill 读）
_ARCHIVE_SCORE_THRESHOLD = 60   # 估值分≥此值才入档案（召回门槛）
_ARCHIVE_FRESH_HOURS = int(os.environ.get("VALUATION_ARCHIVE_FRESH_HOURS", "24"))  # 内跳过 Tavily，复用档案 evidence
_ARCHIVE_RECALL_CAP = 30         # 每板块召回上限（按本板块最新 val 降序）
_ARCHIVE_MAX_AGE_DAYS = 90       # 档案 last_run 超龄淘汰
_EVIDENCE_POOL_CAP = 6           # 每维度 evidence 池容量
_EVIDENCE_MAX_AGE_DAYS = 30      # evidence 超龄淘汰
_SCORE_HISTORY_CAP = 10          # 分数走势保留条数


def _save_archive(arc: dict) -> None:
    try:
        _archive_path().write_text(
            json.dumps(arc, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[valuation-lens] 档案写盘失败: {e}", file=sys.stderr)


def _archive_is_fresh(entry: dict) -> bool:
    """档案 last_run 在 _ARCHIVE_FRESH_HOURS 内且有 evidence_pool → 可跳过 Tavily。"""
    if not entry:
        return False
    last_run = entry.get("last_run")
    pool = entry.get("evidence_pool") or {}
    if not last_run or not (pool.get("S") or pool.get("F") or pool.get("D")):
        return False
    try:
        age_h = (datetime.now() - datetime.fromisoformat(last_run)).total_seconds() / 3600
        return age_h < _ARCHIVE_FRESH_HOURS
    except Exception:
        return False


def _recall_archive_candidates(sector: str) -> List[dict]:
    """召回该板块历史评分不错的标的（档案 score_history 中本板块最新 val≥60）。"""
    sec_key = config.to_under(sector)
    arc = _load_archive()
    scored = []
    for code, e in arc.items():
        hist = e.get("score_history") or []
        sec_scores = [h for h in hist if h.get("sector") == sec_key]
        if not sec_scores:
            continue
        latest = sec_scores[-1]
        if (latest.get("val") or 0) < _ARCHIVE_SCORE_THRESHOLD:
            continue
        scored.append((latest.get("val") or 0, code, e))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, code, e in scored[:_ARCHIVE_RECALL_CAP]:
        out.append({"code": code, "name": e.get("name", ""), "source": "archive",
                    "segment_hint": e.get("segment", sector)})
    return out


def _cailianshe_per_stock(stock_name: str, code: str, limit: int = 3,
                          last_run: Optional[str] = None, days: int = 14) -> List[dict]:
    """拉财联社近期新闻中提及该股的条目（实时 D/F 证据）。复用 HERMES_NEWS_JSON。

    按股票名（≥2 字）或代码过滤，返回 [{title, text, publish_time, is_new}]。
    days 限制回看窗口（publish_time 早于 now-days 的跳过）。
    is_new = publish_time 晚于 last_run（上次跑的时间），用于增量标记。
    阈值 2（非 3）：恢复 2 字名标的召回，误匹配由 limit + 后续评分兜底。
    """
    if not stock_name and not code:
        return []
    try:
        news = (json.loads(config.HERMES_NEWS_JSON.read_text(encoding="utf-8"))).get("news") or []
    except Exception:
        return []
    name = stock_name or ""
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for n in news:
        pt = n.get("publish_time", "") or ""
        if pt:  # 早于回看窗口的跳过；parse 失败则保留（不误删）
            try:
                if datetime.fromisoformat(pt.replace("Z", "")) < cutoff:
                    continue
            except Exception:
                pass
        title = str(n.get("title", ""))
        content = str(n.get("content", ""))[:600]
        text = title + " " + content
        if name and len(name) >= 2 and name in text:
            pass
        elif code and code in text:
            pass
        else:
            continue
        is_new = bool(last_run and pt and pt > last_run)
        out.append({"title": title, "text": _snippet(content) or content[:200],
                    "publish_time": pt, "is_new": is_new})
        if len(out) >= limit:
            break
    return out


def _merge_pool(existing: list, new_items: list) -> list:
    """合并 evidence 池：追加新条目，按 text 前 60 字去重，按 collected_at 降序保留 top N，超龄淘汰。"""
    by_key = {}
    for it in (existing or []):
        k = (it.get("text") or "")[:60]
        if k and k not in by_key:
            by_key[k] = it
    for it in (new_items or []):
        k = (it.get("text") or "")[:60]
        if k and k not in by_key:
            by_key[k] = it
    cutoff = (datetime.now() - timedelta(days=_EVIDENCE_MAX_AGE_DAYS)).isoformat()
    items = [it for it in by_key.values() if (it.get("collected_at") or "") >= cutoff]
    items.sort(key=lambda it: it.get("collected_at") or "", reverse=True)
    return items[:_EVIDENCE_POOL_CAP]


def _upsert_archive(sector: str, scored_candidates: List[dict],
                    evidence_map: Dict[str, dict]) -> None:
    """把本次评分不错的标的 upsert 进知识档案。

    - used_archive（24h 内已跑过）：S/F 池保留旧值（Tavily 复用），D 池合并本次财联社
    - 否则：S/F/D 池合并本次全新 Tavily+财联社
    - key_facts 用本次 LLM reason 覆盖（最新综合）
    - score_history 追加本次（sector, s/f/d/val）
    - last_run/runs 更新；超龄档案淘汰
    """
    if not scored_candidates:
        return
    sec_key = config.to_under(sector)
    arc = _load_archive()
    now_iso = datetime.now().isoformat()
    n_upserted = 0
    for c in scored_candidates:
        vs = c.get("valuation_score")
        if not isinstance(vs, (int, float)) or vs < _ARCHIVE_SCORE_THRESHOLD:
            continue
        code = c.get("stock_code") or c.get("code")
        if not code:
            continue
        e = arc.get(code) or {}
        sr = (evidence_map.get(code) or {})
        new_pool = sr.get("new_pool") or {}
        used_archive = sr.get("used_archive")
        existing_pool = e.get("evidence_pool") or {}
        merged_pool = {}
        for dim in ("S", "F", "D"):
            ex = existing_pool.get(dim) or []
            nw = new_pool.get(dim) or []
            if used_archive and dim in ("S", "F"):
                merged_pool[dim] = ex  # Tavily 复用档案，S/F 不重搜
            else:
                merged_pool[dim] = _merge_pool(ex, nw)
        key_facts = {
            "S": _strip_ev((c.get("scarcity") or {}).get("reason", "")),
            "F": _strip_ev((c.get("forward") or {}).get("reason", "")),
            "D": _strip_ev((c.get("supply_demand") or {}).get("reason", "")),
        }
        hist = (e.get("score_history") or [])
        hist.append({"run": now_iso, "sector": sec_key,
                     "s": (c.get("scarcity") or {}).get("score"),
                     "f": (c.get("forward") or {}).get("score"),
                     "d": (c.get("supply_demand") or {}).get("score"),
                     "val": vs})
        hist = hist[-_SCORE_HISTORY_CAP:]
        sectors_seen = sorted(set((e.get("sectors_seen") or []) + [sec_key]))
        arc[code] = {
            "name": c.get("company") or c.get("name", ""),
            "last_run": now_iso,
            "runs": (e.get("runs") or 0) + 1,
            "last_pe": c.get("pe"),
            "role": c.get("role", e.get("role", "")),
            "segment": c.get("segment", e.get("segment", "")),
            "key_facts": key_facts,
            "evidence_pool": merged_pool,
            "score_history": hist,
            "key_risks": c.get("key_risks") or e.get("key_risks") or [],
            "sectors_seen": sectors_seen,
        }
        # 保留 deep-analyze 写入的 deep_* 字段（跨 skill 积累不互通会丢；维度不同不合并）
        for dk in ("deep_key_facts", "deep_score_history", "deep_last_run", "deep_runs"):
            if dk in e:
                arc[code][dk] = e[dk]
        n_upserted += 1
    cutoff = (datetime.now() - timedelta(days=_ARCHIVE_MAX_AGE_DAYS)).isoformat()
    # 超龄淘汰：取 last_run（本 skill）与 deep_last_run（deep-analyze）中较新者判断，
    # 避免只有 deep_* 的条目（无 last_run）被误淘汰（跨 skill 积累互通）
    def _latest_activity(v):
        ts = [t for t in (v.get("last_run"), v.get("deep_last_run")) if t]
        return max(ts) if ts else ""
    arc = {k: v for k, v in arc.items() if _latest_activity(v) >= cutoff}
    _save_archive(arc)
    print(f"[valuation-lens] 档案 upsert {sec_key}: 档案共 {len(arc)} 只"
          f"（本次≥{_ARCHIVE_SCORE_THRESHOLD}分 {n_upserted} 只）", file=sys.stderr)


# ===== 板块级知识档案（积累板块供需概要，下次注入作"板块历史认知"）=====
def _sector_archive_path():
    return config.OUTPUT_DIR / "valuation_sector_archive.json"


def _load_sector_archive() -> dict:
    try:
        return json.loads(_sector_archive_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_sector_prior(sector: str) -> str:
    """取该板块上次跑的供需概要（LLM preamble）作为板块历史认知。"""
    e = _load_sector_archive().get(config.to_under(sector)) or {}
    return (e.get("summary") or "").strip()


def _synthesize_sector_summary(candidates: List[dict]) -> str:
    """从候选的供需理由合成板块概要（当 LLM 未给 preamble 时的兜底）。"""
    parts = []
    for c in (candidates or [])[:8]:
        nm = c.get("company") or c.get("name", "")
        sd = (c.get("supply_demand") or {}).get("reason", "")
        if sd:
            parts.append(f"- {nm}: {sd[:80]}")
    return "\n".join(parts)[:2000]


def _upsert_sector_archive(sector: str, summary: Optional[str]) -> None:
    if not sector or not summary or not str(summary).strip():
        return
    sec_key = config.to_under(sector)
    d = _load_sector_archive()
    d[sec_key] = {"summary": str(summary).strip()[:4000],
                  "last_run": datetime.now().isoformat()}
    try:
        _sector_archive_path().write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[valuation-lens] 板块档案写盘失败: {e}", file=sys.stderr)
    print(f"[valuation-lens] 板块档案 upsert {sec_key}: summary {len(str(summary))}字",
          file=sys.stderr)
