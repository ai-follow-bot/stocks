"""report-judge 评判逻辑（SPEC §7）：读报告 + 元数据 -> LLM -> 结构化输出。

- extract_task_meta: filename task_id + SQLite（best-effort）+ 同名 .json + 报告头部扫描。
- get_judge_client: JUDGE_LLM_PROVIDER 控制，默认 auto = 优先用与报告不同的模型。
- judge_report: LLM 评判 -> json_from_llm -> 代码按 weight 确定性合成总分 + 等级。
失败返回 {quality_score: null, error}，不阻塞报告。
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from chain_agent import config
from chain_agent.llm.client import get_llm_client, AnthropicClient, OpenAICompatibleClient
from chain_agent.llm.parse import json_from_llm

from .rubric import RUBRIC, WEIGHTS, DIM_NAMES, compose_total, grade_from_total
from .prompts import JUDGE_SYSTEM, JUDGE_USER_TEMPLATE
from . import archive

# 报告全文喂给 LLM 的截断长度（字符）
REPORT_TRUNCATE = 20000

# task SQLite 路径（best-effort 读取，缺失/不可读则降级）
# db.ts 的 dbDir = services/../../../data -> /home/smallsite-vue/data/followbot.db
STOCKS_DB_PATH = os.environ.get(
    "STOCKS_DB_PATH", "/home/smallsite-vue/data/followbot.db"
)

# 改进队列人工处置状态（admin-stocks.ts 写）：读已 applied 的 keyword_add 做正反馈命中检查
IMPROVEMENTS_STATUS_PATH = config.OUTPUT_DIR / "improvements_status.json"

# 报告生成的 LLM 模型 -> 评判应选的「不同」provider（auto 模式用）
# report 用 glm -> judge 用 openai(DeepSeek)；report 用 deepseek/kimi -> judge 用 anthropic(GLM)
_REPORT_TO_JUDGE_PROVIDER = {
    "glm": "openai",       # 报告 GLM -> 评判 DeepSeek
    "deepseek": "anthropic",  # 报告 DeepSeek -> 评判 GLM
    "kimi": "anthropic",   # 报告 Kimi -> 评判 GLM
}

# 报告头部关键词 -> task_type（SQLite 不可用时的兜底）
_HEADER_TASK_TYPE_HINTS = [
    ("431 中国特色", "ce_value"),
    ("中国特色价值投资", "ce_value"),
    ("周期镜头", "cycle_stock"),
    ("业绩-估值周期", "cycle_stock"),
    ("估值镜头", "valuation"),
    ("产业链深度拆解", "deep_chain"),
    ("深度拆解", "deep_chain"),
    ("三视角", "harness"),
]


def _extract_task_id(basename: str) -> Optional[int]:
    """从文件名 `<prefix>_<ts>_<id>.<ext>` 抠 task_id。"""
    m = re.search(r"_(\d+)\.(?:md|json)$", basename)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _prefix_task_type_hints(basename: str) -> dict:
    """从文件名前缀推断 market/task_type/sector（兜底，SQLite 优先）。"""
    meta = {}
    name = basename
    if name.startswith("us_"):
        meta["market"] = "us"
        name = name[3:]
    # stock 类前缀
    for pref, ttype in [
        ("valuation_stock_", "valuation_stock"),
        ("harness_stock_", "harness_stock"),
        ("cycle_stock_", "cycle_stock"),
        ("stock_", "stock"),
    ]:
        if name.startswith(pref):
            meta["task_type"] = ttype
            meta["stock_input"] = name[len(pref):].rsplit("_", 2)[0] if "_" in name[len(pref):] else name[len(pref):]
            return meta
    if name.startswith("batch_"):
        meta["task_type"] = "chain"  # batch 走 chain 模块
        return meta
    # 其余：sector 取第一个 _ 之前
    sector = name.split("_", 1)[0] if "_" in name else name
    if sector:
        meta["sector"] = sector
    return meta


def _sqlite_lookup(task_id: int) -> dict:
    """best-effort 查 stocks_tasks，缺失/不可读返回 {}。"""
    if not task_id:
        return {}
    if not os.path.exists(STOCKS_DB_PATH):
        return {}
    try:
        con = sqlite3.connect(f"file:{STOCKS_DB_PATH}?mode=ro", uri=True, timeout=3)
        cur = con.execute(
            "SELECT task_type, sector, days, llm_model, market, stock_input "
            "FROM stocks_tasks WHERE id=?",
            (task_id,),
        )
        row = cur.fetchone()
        con.close()
        if not row:
            return {}
        return {
            "task_type": row[0],
            "sector": row[1],
            "days": row[2],
            "llm_model": row[3],
            "market": row[4],
            "stock_input": row[5],
        }
    except Exception as e:
        print(f"[report-judge] SQLite 查询失败 (task_id={task_id}): {e}", file=sys.stderr)
        return {}


def _scan_report_header(text: str) -> dict:
    """扫描报告头部（前 60 行）兜底补 task_type/sector/data_quality/days。"""
    meta = {}
    head = "\n".join(text.splitlines()[:60])
    for kw, ttype in _HEADER_TASK_TYPE_HINTS:
        if kw in head:
            meta["task_type"] = ttype
            break
    # data_quality：降级标记
    if re.search(r"降级|degraded|数据质量[:：]\s*(degraded|差|不足)", head):
        meta["data_quality"] = "degraded"
    elif "数据质量" in head:
        meta["data_quality"] = "normal"
    # days/窗口
    m = re.search(r"(?:窗口|数据窗口|回看)\D{0,3}(\d+)\s*天", head)
    if m:
        try:
            meta["days"] = int(m.group(1))
        except ValueError:
            pass
    # sector：标题里的板块名（# XXX 产业链 / # XXX 镜头）
    m = re.search(r"^#\s*(.+?)(?:产业链|深度拆解|周期镜头|估值镜头|中国特色)", head, re.MULTILINE)
    if m:
        meta["sector"] = m.group(1).strip().strip("（(").strip()
    return meta


def _pipeline_json_meta(filepath: Path) -> dict:
    """若报告是 .md 且同名 .json 存在（chain --json 跑过），抠 data_quality/候选数/evidence 数。
    若报告本身是 .json（chain no-llm），直接解析它。"""
    meta = {}
    target = None
    if filepath.suffix == ".json":
        target = filepath
    else:
        sibling = filepath.with_suffix(".json")
        if sibling.exists():
            target = sibling
    if not target:
        return meta
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return meta
    if not isinstance(data, dict):
        return meta
    if "data_quality" in data:
        meta["data_quality"] = data["data_quality"]
    # 候选数：candidates / results / scored_candidates 等常见键
    for k in ("candidates", "scored_candidates", "results", "top_candidates"):
        v = data.get(k)
        if isinstance(v, list):
            meta["candidate_count"] = len(v)
            break
    return meta


def extract_task_meta(filepath) -> dict:
    """best-effort 合并 task 元数据：SQLite（权威）+ filename 前缀 + 同名 .json + 报告头部。

    返回 {task_id, task_type, sector, days, data_quality, llm_model, market, stock_input,
          candidate_count}，各字段可能缺失。
    """
    fp = Path(filepath)
    basename = fp.name
    meta = {"filename": basename}

    task_id = _extract_task_id(basename)
    if task_id:
        meta["task_id"] = task_id

    # 1. filename 前缀兜底
    meta.update(_prefix_task_type_hints(basename))

    # 2. SQLite 权威覆盖
    if task_id:
        sq = _sqlite_lookup(task_id)
        if sq:
            for k, v in sq.items():
                if v is not None and v != "":
                    meta[k] = v

    # 3. 报告头部补缺（sector/data_quality/days/task_type 仍缺时）
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception:
        text = ""
    if text:
        header_meta = _scan_report_header(text)
        for k, v in header_meta.items():
            if k not in meta or meta.get(k) in (None, ""):
                meta[k] = v
        # 4. 同名 .json 补 data_quality/候选数
        pj = _pipeline_json_meta(fp)
        for k, v in pj.items():
            if k == "data_quality":
                meta[k] = v  # json 里的 data_quality 优先于头部
            elif k not in meta:
                meta[k] = v

    # 规整 llm_model：DB 可能存 'glm'/'kimi'/'deepseek'
    return meta


def _resolve_judge_provider(task_meta: Optional[dict]) -> str:
    """决定评判用的 LLM provider。

    JUDGE_LLM_PROVIDER 显式设为 anthropic/openai/kimi/none -> 直接用。
    默认 auto：优先用与报告不同的模型（report glm->judge deepseek；report deepseek/kimi->judge glm；
    未知 -> GLM，对齐 spec「优先 GLM」）。
    """
    explicit = os.environ.get("JUDGE_LLM_PROVIDER", "").strip().lower()
    if explicit in ("anthropic", "openai", "kimi", "none"):
        return explicit
    report_model = (task_meta or {}).get("llm_model", "")
    if isinstance(report_model, str):
        report_model = report_model.lower()
    return _REPORT_TO_JUDGE_PROVIDER.get(report_model, "anthropic")


def get_judge_client(task_meta: Optional[dict] = None):
    """返回 (client, provider_label)。provider_label 是实际用的 provider 名（用于存档）。

    通过临时改 config.LLM_PROVIDER 复用 get_llm_client 的降级链。judge 跑在独立进程，
    全局 mutation 无副作用。
    """
    provider = _resolve_judge_provider(task_meta)
    saved = config.LLM_PROVIDER
    try:
        config.LLM_PROVIDER = provider
        client = get_llm_client()
    finally:
        config.LLM_PROVIDER = saved
    if client is None:
        return None, provider
    # 实际 provider 标签：拿到哪个 client 就标哪个
    if isinstance(client, AnthropicClient):
        label = "anthropic(glm)"
    elif isinstance(client, OpenAICompatibleClient):
        # openai 兼容可能是 deepseek 或 kimi，按 config.OPENAI_MODEL 标
        label = f"openai({config.OPENAI_MODEL})"
    else:
        label = provider
    return client, label


def _normalize_dimensions(raw_dims: list) -> list:
    """规整 LLM 返回的 dimensions：补 name、clamp score、按 RUBRIC 顺序对齐、补缺维。"""
    by_key = {}
    if isinstance(raw_dims, list):
        for d in raw_dims:
            if not isinstance(d, dict):
                continue
            key = d.get("key")
            if key in WEIGHTS:
                by_key[key] = d
    out = []
    for r in RUBRIC:
        d = by_key.get(r["key"], {})
        score = d.get("score")
        try:
            score = int(round(float(score))) if score is not None else 0
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        issues = d.get("issues") or []
        if isinstance(issues, str):
            issues = [issues]
        issues = [str(x) for x in issues if x]
        out.append({
            "key": r["key"],
            "name": r["name"],
            "score": score,
            "reason": str(d.get("reason") or "").strip(),
            "issues": issues,
        })
    return out


# action_items target 合法取值（按可否自动应用分组）
_ACTION_TARGETS = {
    "keyword_add", "keyword_remove",            # 可自动应用（改 sector_keywords.json）
    "core_company_add", "core_company_remove",  # 半自动（仅 review）
    "prompt_synth", "prompt_conclusion", "prompt_risk", "search_depth",  # 仅 review
}
_SEVERITIES = {"high", "medium", "low"}


def _normalize_action_items(raw_items, fallback_sector: str = "") -> list:
    """规整 LLM 返回的 action_items：校验 target/severity，补 sector。"""
    if not isinstance(raw_items, list):
        return []
    out = []
    seen = set()
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        target = it.get("target")
        if target not in _ACTION_TARGETS:
            continue
        value = str(it.get("value") or "").strip()
        if not value or len(value) > 200:
            continue
        sector = str(it.get("sector") or fallback_sector or "").strip()
        severity = it.get("severity")
        if severity not in _SEVERITIES:
            severity = "medium"
        rationale = str(it.get("rationale") or "").strip()
        source_dim = str(it.get("source_dim") or "").strip()
        # 同 (target, sector, value) 去重
        fp = f"{target}|{sector}|{value}"
        if fp in seen:
            continue
        seen.add(fp)
        out.append({
            "target": target,
            "sector": sector,
            "value": value,
            "severity": severity,
            "rationale": rationale,
            "source_dim": source_dim,
        })
    return out


def _applied_keywords_for_sector(sector: str) -> list:
    """读 improvements_status.json，返回该 sector 已 applied 的 keyword_add value 列表。

    用于正反馈验证：这些是人工已应用到 sector_keywords.json 的关键词，检查新报告是否真采到。
    防御：文件缺失/不可读 -> []（graceful，不阻塞评判）。
    """
    if not sector:
        return []
    try:
        if not IMPROVEMENTS_STATUS_PATH.exists():
            return []
        data = json.loads(IMPROVEMENTS_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    sector_norm = str(sector).strip().lower()
    out = []
    for _fp, st in data.items():
        if not isinstance(st, dict):
            continue
        if st.get("status") != "applied" or st.get("target") != "keyword_add":
            continue
        if str(st.get("sector") or "").strip().lower() != sector_norm:
            continue
        v = str(st.get("value") or "").strip()
        if v:
            out.append(v)
    return out


def _keyword_hit_check(keyword: str, report_text: str) -> tuple:
    """检查 keyword 是否在 report_text 命中。按 / ， 、 , 空格 拆 token，任一命中即 True。

    返回 (present: bool, matched_token: str|None)。keyword 如「球硅/球形氧化铝」或
    「安集科技 CMP抛光液」拆成 ['球硅','球形氧化铝'] / ['安集科技','CMP抛光液']，任一子串
    命中即可（避免因复合词整体未出现而误判未覆盖）。
    """
    if not keyword or not report_text:
        return False, None
    tokens = [t.strip() for t in re.split(r"[/，、,\s]+", keyword) if t.strip()]
    if not tokens:
        tokens = [keyword.strip()]
    for tok in tokens:
        if tok and tok in report_text:
            return True, tok
    return False, None


def judge_report(filepath: str, task_meta: dict = None) -> dict:
    """读报告 + 元数据 -> LLM 评判 -> 结构化输出（SPEC §7）。

    返回 {
        quality_score: "A"|"B"|"C"|"D"|None,
        total_score: int,
        dimensions: [{key, name, score, reason, issues}],
        cross_path_conflicts: [str],
        suggestions: [str],
        action_items: [{target, sector, value, severity, rationale, source_dim}],
        applied_keyword_hits: [{keyword, present, matched_token}],  # 正反馈验证
        judged_at: iso,
        llm_provider: str,
        filename: str,
    }
    失败返回 {quality_score: None, error: ..., judged_at, llm_provider, filename}。
    """
    fp = Path(filepath)
    basename = fp.name
    judged_at = datetime.now().isoformat()

    # 元数据（调用方未传则自动抽取）
    if task_meta is None:
        task_meta = extract_task_meta(fp)

    # 读报告全文
    try:
        report_text = fp.read_text(encoding="utf-8")
    except Exception as e:
        return {"quality_score": None, "error": f"读报告失败: {e}",
                "judged_at": judged_at, "llm_provider": None, "filename": basename}

    # .json 报告：把 JSON 美化成可读文本喂 LLM（chain no-llm 场景）
    if fp.suffix == ".json":
        try:
            data = json.loads(report_text)
            report_text = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 解析失败就用原文

    report_truncated = report_text[:REPORT_TRUNCATE]
    if len(report_text) > REPORT_TRUNCATE:
        report_truncated += "\n\n[... 报告过长，已截断 ...]"

    client, provider_label = get_judge_client(task_meta)
    if client is None:
        return {"quality_score": None, "error": "评判 LLM 不可用",
                "judged_at": judged_at, "llm_provider": provider_label, "filename": basename}

    from .rubric import rubric_text
    user = JUDGE_USER_TEMPLATE.format(
        task_type=task_meta.get("task_type") or "未知",
        sector=task_meta.get("sector") or "未知",
        data_quality=task_meta.get("data_quality") or "未知",
        llm_model=task_meta.get("llm_model") or "未知",
        rubric_text=rubric_text(),
        report_text=report_truncated,
    )

    try:
        raw = client.synthesize(JUDGE_SYSTEM, user, temperature=config.JUDGE_TEMPERATURE)
    except Exception as e:
        return {"quality_score": None, "error": f"LLM 调用失败: {e}",
                "judged_at": judged_at, "llm_provider": provider_label, "filename": basename}

    if not raw:
        return {"quality_score": None, "error": "LLM 返回空",
                "judged_at": judged_at, "llm_provider": provider_label, "filename": basename}

    data = json_from_llm(raw)
    if not isinstance(data, dict):
        # GLM 偶发不输出 JSON（散文/截断），重试一次并加严约束
        print(f"[report-judge] JSON 解析失败，重试一次: {raw[:200]}", file=sys.stderr)
        try:
            raw = client.synthesize(
                JUDGE_SYSTEM + "\n\n再次提醒：只输出一个 JSON 对象，不要任何前后文字、代码块或解释。",
                user, temperature=config.JUDGE_TEMPERATURE,
            )
            if raw:
                data = json_from_llm(raw)
        except Exception as e:
            print(f"[report-judge] 重试失败: {e}", file=sys.stderr)
    if not isinstance(data, dict):
        return {"quality_score": None, "error": "LLM 输出 JSON 解析失败",
                "judged_at": judged_at, "llm_provider": provider_label, "filename": basename}

    dimensions = _normalize_dimensions(data.get("dimensions"))
    total_score = compose_total(dimensions)
    grade = grade_from_total(total_score)

    conflicts = data.get("cross_path_conflicts") or []
    if isinstance(conflicts, str):
        conflicts = [conflicts]
    conflicts = [str(x) for x in conflicts if x]

    suggestions = data.get("suggestions") or []
    if isinstance(suggestions, str):
        suggestions = [suggestions]
    suggestions = [str(x) for x in suggestions if x]

    action_items = _normalize_action_items(
        data.get("action_items"),
        fallback_sector=(task_meta or {}).get("sector") or "",
    )

    # 正反馈验证：该 sector 已 applied 的 keyword_add 是否在新报告命中（硬信号）
    # 命中=keyword_add 真让 pipeline 采到该词；未命中=执行问题（转 search_depth/prompt）
    applied_kw = _applied_keywords_for_sector((task_meta or {}).get("sector") or "")
    applied_keyword_hits = []
    for kw in applied_kw:
        present, matched = _keyword_hit_check(kw, report_text)
        applied_keyword_hits.append({
            "keyword": kw, "present": present, "matched_token": matched,
        })

    return {
        "quality_score": grade,
        "total_score": total_score,
        "dimensions": dimensions,
        "cross_path_conflicts": conflicts,
        "suggestions": suggestions,
        "action_items": action_items,
        "applied_keyword_hits": applied_keyword_hits,
        "judged_at": judged_at,
        "llm_provider": provider_label,
        "filename": basename,
    }
