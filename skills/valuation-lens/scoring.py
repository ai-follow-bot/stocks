"""valuation-lens 评分：PE 方向约束（确定性）+ LLM 三维打分。

PE 方向（low/neutral/high）由 _compute_pe_verdicts 基于当批 PE 分布确定性计算（非 LLM）；
估值分由 _compute_valuation_score 软阈值连续模型算（三高标的 PE 不参与）。
LLM 三维打分分批进行，整批失败时逐只重试。
"""

import json
import os
import sys
from typing import Dict, List, Optional

from chain_agent.llm.client import get_llm_client
from chain_agent.llm.parse import split_text_and_json

from . import prompts


# ===== 工具 =====
def _num(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _llm_call_meta(system: str, user: str) -> dict:
    client = get_llm_client()
    if client is None:
        return {"text": None, "stop_reason": None}
    try:
        return client.synthesize_with_meta(system, user)
    except Exception as e:
        print(f"[valuation-lens] LLM 调用失败: {e}", file=sys.stderr)
        return {"text": None, "stop_reason": None}


# ===== PE 方向约束 + 估值分（确定性，软阈值连续模型）=====
def _ramp(v: float, lo: float, hi: float) -> float:
    """0 当 v<=lo，1 当 v>=hi，中间线性过渡（软阈值）。"""
    if v <= lo:
        return 0.0
    if v >= hi:
        return 1.0
    return (v - lo) / (hi - lo)


# PE 调整表：{pe_verdict: {strong/middle/weak: adj}}
_PE_ADJ = {
    "high":   {"strong": 0,  "middle": -5, "weak": -10},
    "low":    {"strong": 10, "middle": 8,  "weak": 0},
    "neutral":{"strong": 0,  "middle": 0,  "weak": 0},
    "null":   {"strong": 5,  "middle": 0,  "weak": -5},
}


def _compute_pe_verdicts(candidates: list) -> None:
    """确定性计算每个候选的 pe_context.verdict（基于当批 PE 分布，非 LLM 主观）。

    PE < median*0.75 → low；> median*1.33 → high；其余 neutral；缺失 → null。
    当批 PE 不足 4 个时退化为绝对阈值（<30 low, >80 high）——这是方法论常量，
    非 per-stock 手填参数。
    """
    pes = [c.get("pe") for c in candidates]
    pes = [p for p in pes if isinstance(p, (int, float)) and p > 0]

    if len(pes) >= 4:
        ps = sorted(pes)
        mid = len(ps) // 2
        median = ps[mid] if len(ps) % 2 else (ps[mid - 1] + ps[mid]) / 2
        lo_thr, hi_thr = median * 0.75, median * 1.33

        def verdict(p):
            if not isinstance(p, (int, float)) or p <= 0:
                return "null"
            return "low" if p < lo_thr else ("high" if p > hi_thr else "neutral")
    else:
        def verdict(p):
            if not isinstance(p, (int, float)) or p <= 0:
                return "null"
            return "low" if p < 30 else ("high" if p > 80 else "neutral")

    for c in candidates:
        pe_ctx = c.get("pe_context") or {}
        pe_ctx["verdict"] = verdict(c.get("pe"))
        c["pe_context"] = pe_ctx


def _pe_note(triple_f: float, strong_f: float, weak_f: float, pe_v: str) -> str:
    """由档位权重 + PE verdict 生成人类可读的 PE 处理说明。"""
    if triple_f >= 0.9:
        return "三高标的（稀缺/前瞻/供需均强），PE 不参与调整"
    if triple_f >= 0.5:
        return "接近三高，PE 影响大幅减弱"
    if weak_f >= 0.5:
        if pe_v == "high":
            return "高PE扣分（三信号弱，价值陷阱嫌疑）"
        if pe_v == "low":
            return "低PE未加分（三信号弱）"
        return "三信号弱，PE 中性"
    if strong_f >= 0.5:
        if pe_v == "high":
            return "高PE未扣分（稀缺/前瞻强，re-rating合理）"
        if pe_v == "low":
            return "低PE锦上添花（稀缺/前瞻强）"
        return "稀缺/前瞻强，PE 中性"
    if pe_v == "high":
        return "高PE小幅扣分"
    if pe_v == "low":
        return "低PE小幅加分"
    return "PE 中性"


def _compute_valuation_score(c: dict) -> tuple:
    """确定性计算 valuation_score + pe_treatment（连续模型，软阈值）。

    valuation_score = 0.35*scarcity + 0.30*forward + 0.25*supply_demand + pe_adj
    （base 0..90，pe_adj ∈ [-10,+10]，clamp 0..100）

    PE 调整（连续，非离散跳变）：
    - strong/middle/weak 档权重由 max(s,f) 与 min(s,f,d) 在过渡带 [70,75]/[45,50] 软决定
    - base_adj = Σ 档权重 × 该档 PE adj
    - triple_factor = min(s,f,d) 在 [65,70] 软过渡；=1 时 pe_adj 归零（三高标的 PE 不参与）
    - pe_adj = base_adj × (1 - triple_factor)
    - pe_context.verdict 由 _compute_pe_verdicts 确定性给出（非 LLM）
    """
    sc = c.get("scarcity") or {}
    fw = c.get("forward") or {}
    sd = c.get("supply_demand") or {}
    s = _num(sc.get("score"), 50)
    f = _num(fw.get("score"), 50)
    d = _num(sd.get("score"), 50)
    pe_v = ((c.get("pe_context") or {}).get("verdict") or "null").lower()

    strong_f = max(_ramp(s, 70, 75), _ramp(f, 70, 75))
    weak_f = 1 - max(_ramp(s, 45, 50), _ramp(f, 45, 50), _ramp(d, 45, 50))
    middle_f = max(0.0, 1.0 - strong_f - weak_f)
    total = strong_f + middle_f + weak_f
    if total > 0:
        strong_f, middle_f, weak_f = strong_f / total, middle_f / total, weak_f / total

    adj_table = _PE_ADJ.get(pe_v, _PE_ADJ["neutral"])
    base_adj = (strong_f * adj_table["strong"] + middle_f * adj_table["middle"]
                + weak_f * adj_table["weak"])

    triple_f = min(_ramp(s, 65, 70), _ramp(f, 65, 70), _ramp(d, 65, 70))
    adj = base_adj * (1 - triple_f)

    base = 0.35 * s + 0.30 * f + 0.25 * d
    score = max(0, min(100, round(base + adj)))
    note = _pe_note(triple_f, strong_f, weak_f, pe_v)
    return score, note, strong_f, weak_f


# ===== LLM 三维打分（分批）=====
def _slim_candidate(c: dict) -> dict:
    """给 LLM 看的精简候选 dict（去掉 None/空）。"""
    keep = {}
    for k, v in c.items():
        if v in (None, "", []):
            continue
        keep[k] = v
    return keep


def score_valuations(chain_name: str, candidates: List[dict],
                     evidence_map: Dict[str, dict],
                     sector_prior: Optional[str] = None) -> dict:
    """对候选做 LLM 三维打分，返回 {candidates, supply_demand_analysis?}。"""
    if not candidates:
        return {"candidates": [], "note": "no candidates"}

    BATCH = max(2, int(os.environ.get("VALUATION_LENS_BATCH", "4")))
    batches = [candidates[i:i + BATCH] for i in range(0, len(candidates), BATCH)]
    print(f"[valuation-lens] 评分分批: {len(candidates)} 只 / {len(batches)} 批 "
          f"(batch_size={BATCH})", file=sys.stderr)

    out: list = []
    raw_snippets: list = []
    batch_preambles: dict = {}

    def _call_llm_batch(batch):
        """对一批候选调 LLM 打分。返回 (parsed_list, preamble, fail_raw)；parsed_list 空=失败。"""
        parts = []
        for c in batch:
            sr = (evidence_map.get(c.get('code'), {}) or {})
            head = f"## {c.get('name') or c.get('code')}（{c.get('code')}）"
            prior = ""
            kf = sr.get("key_facts") or {}
            if kf.get("S") or kf.get("F") or kf.get("D"):
                prior = "\n[历史结论 prior（档案上次综合，须用本次 evidence 增量更新，勿照搬）]\n" \
                        f"  稀缺: {kf.get('S','')}\n  前瞻: {kf.get('F','')}\n  供需: {kf.get('D','')}"
            if sr.get("used_archive"):
                prior += "\n[本次 24h 内已搜过：Tavily 复用档案证据，财联社为最新实时（[新增]=自上次以来新发）]"
            else:
                pp = sr.get("prev_pool") or {}
                hist_items = []
                for dim in ("S", "F", "D"):
                    for it in (pp.get(dim) or [])[:2]:
                        hist_items.append(f"  [{dim}档案] {(it.get('text') or '')[:120]}")
                if hist_items:
                    prior += "\n[历史证据（档案池，补充参考）]\n" + "\n".join(hist_items[:6])
            parts.append(f"{head}{prior}\n{sr.get('content_text','')}")
        evidence_text = "\n\n".join(parts)
        slim = [_slim_candidate(c) for c in batch]
        sp = (f"\n# 板块历史认知（档案上次供需概要，供参考，须用本次 evidence 增量更新）\n{sector_prior}\n"
              if sector_prior else "")
        user = prompts.VALUATION_USER_TEMPLATE.format(
            chain_name=chain_name,
            candidates=json.dumps(slim, ensure_ascii=False, indent=2),
            evidence_text=evidence_text[:24000],
            sector_prior=sp,
        )
        meta = _llm_call_meta(prompts.VALUATION_SYSTEM, user)
        text = meta.get("text") or ""
        if not text:
            return [], "", ""
        preamble, data = split_text_and_json(text)
        if not data:
            # 解析失败：不收 preamble（split_text_and_json fallback 会把整段文本当 preamble，那是垃圾）
            return [], "", text[:500]
        batch_out = data if isinstance(data, list) else (
            data.get("candidates") or data.get("segments") or ([data] if data.get("stock_code") else [])
        )
        return batch_out, preamble or "", ""

    for idx, batch in enumerate(batches, 1):
        batch_out, preamble, fail_raw = _call_llm_batch(batch)
        if preamble and len(preamble) > len(batch_preambles.get("text", "")):
            batch_preambles["text"] = preamble
        if batch_out:
            print(f"[valuation-lens] 批 {idx}/{len(batches)} 解析出 {len(batch_out)} 只", file=sys.stderr)
            out.extend(batch_out)
            continue
        # 整批失败 → 逐只重试（小批次更易成功，规避 max_tokens 截断）
        reason = "无响应" if not fail_raw else "JSON解析失败"
        print(f"[valuation-lens] [warn] 批 {idx}/{len(batches)} {reason}，逐只重试", file=sys.stderr)
        if fail_raw:
            raw_snippets.append(fail_raw)
        for c in batch:
            single_out, sp2, fr2 = _call_llm_batch([c])
            if sp2 and len(sp2) > len(batch_preambles.get("text", "")):
                batch_preambles["text"] = sp2
            if single_out:
                out.extend(single_out)
                print(f"[valuation-lens]   重试 {c.get('code')} 成功", file=sys.stderr)
            else:
                out.append({**c, "stock_code": c["code"]})
                if fr2:
                    raw_snippets.append(fr2)
                print(f"[valuation-lens]   重试 {c.get('code')} 仍失败，留空", file=sys.stderr)

    # 合并输入字段（pe/market_cap/change_pct/source/segment_hint/code/name）
    input_by_code = {c["code"]: c for c in candidates}
    for oc in out:
        code = oc.get("stock_code") or oc.get("code")
        if not code or code not in input_by_code:
            continue
        src = input_by_code[code]
        oc["stock_code"] = code
        oc["code"] = code
        oc.setdefault("name", src.get("name", ""))
        oc.setdefault("company", src.get("name", ""))
        oc.setdefault("pe", src.get("pe"))
        oc.setdefault("market_cap", src.get("market_cap"))
        oc.setdefault("change_pct", src.get("change_pct"))
        oc.setdefault("source", src.get("source"))
        oc.setdefault("mention_count", src.get("mention_count"))
        sr_ev = evidence_map.get(code, {}) or {}
        oc.setdefault("prev_score", sr_ev.get("prev"))
        oc.setdefault("used_archive", sr_ev.get("used_archive"))
        if not oc.get("segment"):
            oc["segment"] = src.get("segment_hint", "")

    # 去重
    seen = set()
    deduped = []
    for oc in out:
        code = oc.get("stock_code") or oc.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        deduped.append(oc)

    # 确定性 PE verdict（基于当批 PE 分布）+ 估值分（软阈值连续模型）
    _compute_pe_verdicts(deduped)
    for oc in deduped:
        score, note, strong_f, weak_f = _compute_valuation_score(oc)
        oc["valuation_score"] = score
        oc["pe_treatment"] = note
        oc["_strong"] = round(strong_f, 2)
        oc["_weak"] = round(weak_f, 2)

    # 过滤明显噪声候选（三维均<40，多半是 StockDetector 从搜索文本误拾的不相关标的）
    def _max_dim(oc):
        vals = [(oc.get(k) or {}).get("score") for k in ("scarcity", "forward", "supply_demand")]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return max(vals) if vals else 0
    noise = [oc for oc in deduped if _max_dim(oc) < 40]
    if noise:
        print(f"[valuation-lens] 过滤 {len(noise)} 只噪声候选（三维均<40）: "
              f"{[oc.get('stock_code') for oc in noise]}", file=sys.stderr)
    deduped = [oc for oc in deduped if _max_dim(oc) >= 40]

    deduped.sort(key=lambda c: c.get("valuation_score", 0), reverse=True)
    data = {"candidates": deduped}
    if batch_preambles.get("text"):
        data["supply_demand_analysis"] = batch_preambles["text"]
    if raw_snippets:
        data["raw_llm_partial"] = "\n---\n".join(raw_snippets)[:2000]
    return data
