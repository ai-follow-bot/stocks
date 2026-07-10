"""ce-value 431 报告渲染：宏观 -> 市场 -> 行业 -> 公司(三高表 + 卡脖子抓手)。"""

import sys


def _v(d: dict, *keys, default="—") -> str:
    """安全取嵌套值，None/空 -> default。"""
    x = d
    for k in keys:
        if not isinstance(x, dict):
            return default
        x = x.get(k)
    if x is None or x == "":
        return default
    return str(x)


def _posture_line(label: str, d: dict, key: str) -> str:
    sub = d.get(key) or {}
    if not isinstance(sub, dict):
        return f"- **{label}**：{default_status(sub)}"
    status = sub.get("status") or "未知"
    note = sub.get("note") or ""
    return f"- **{label}**：{status} — {note}" if note else f"- **{label}**：{status}"


def default_status(v):
    return str(v) if v else "未知"


def _render_macro(m: dict) -> list:
    lines = ["## 一、宏观层（全球 / 政策 / 流动性）", ""]
    if not m or m.get("data_quality") == "degraded":
        lines.append("> 宏观简报降级（LLM 或数据缺失）。")
        return lines + [""]
    lines.append(_posture_line("全球环境", m, "global"))
    lines.append(_posture_line("国内政策", m, "policy"))
    lines.append(_posture_line("流动性", m, "liquidity"))
    fav = m.get("favor_sectors") or []
    if fav:
        lines.append(f"- **宏观顺风方向**：{', '.join(fav)}")
    if m.get("summary"):
        lines.append(f"\n> {m['summary']}")
    return lines + [""]


def _render_market(m: dict) -> list:
    lines = ["## 二、市场层（风格 / 周期 / 资金 / 情绪）", ""]
    if not m or m.get("data_quality") == "degraded":
        lines.append("> 市场简报降级（LLM 或数据缺失）。")
        return lines + [""]
    lines.append(_posture_line("风格", m, "style"))
    lines.append(_posture_line("周期", m, "cycle"))
    lines.append(_posture_line("资金面", m, "capital"))
    lines.append(_posture_line("情绪", m, "sentiment"))
    fav = m.get("favor_style") or []
    if fav:
        lines.append(f"- **市场偏好风格**：{', '.join(fav)}")
    if m.get("summary"):
        lines.append(f"\n> {m['summary']}")
    return lines + [""]


def _render_industry(sectors: list, pick_reason: str) -> list:
    lines = ["## 三、行业层", ""]
    lines.append(f"**选定板块**：{', '.join(sectors)}")
    if pick_reason and pick_reason != "用户指定":
        lines.append(f"\n选择理由：{pick_reason}")
    lines.append("")
    return lines


def _render_company(cr: dict) -> list:
    sec = cr.get("sector", "")
    lines = [f"### 板块：{sec}", ""]

    # 路径成败
    paths = cr.get("paths", {}) or {}
    errs = cr.get("path_errors", {}) or {}
    if paths:
        pstat = " / ".join(f"{k}={'✗' if v else '✓'}" for k, v in paths.items())
        lines.append(f"> 三视角路径：{pstat}　财务命中：{cr.get('financials_hit', '—')}")
        if errs:
            lines.append(f"> 失败详情：{errs}")
        lines.append("")

    # 卡脖子环节
    bn = cr.get("deep_bottlenecks") or {}
    top_bn = bn.get("top_bottlenecks") or []
    segs = [s for s in (bn.get("segments") or []) if s.get("is_bottleneck")]
    lines.append("#### 卡脖子环节（1 抓手）")
    if top_bn:
        lines.append(f"- **瓶颈环节**：{', '.join(top_bn)}")
    elif segs:
        lines.append(f"- **瓶颈环节**：{', '.join(s.get('name','') for s in segs)}")
    else:
        lines.append("- （deep 路径未识别卡脖子环节或失败）")
    if segs:
        for s in segs[:5]:
            lines.append(f"  - {s.get('name','')}（卡脖子分 {s.get('score','—')}）")
    lines.append("")

    # 三高筛选表
    aligned = cr.get("aligned") or []
    lines.append("#### 三高筛选表（3 高）")
    lines.append("")
    lines.append("| 名称 | 高增长 | 高利润 | 高围墙 | 三高综合 | 达标 | chain | deep | val |")
    lines.append("|------|--------|--------|--------|----------|------|-------|------|-----|")
    for a in aligned:
        th = a.get("three_high") or {}
        code = a.get("code", "")
        name = a.get("name", "")
        g = th.get("growth")
        p = th.get("profit")
        m = th.get("moat")
        comp = th.get("composite")
        flag = "✓三高" if th.get("three_high") else ("+".join(th.get("flags", [])) or "—")
        chain_s = _v(a, "chain", "score")
        deep_s = _v(a, "deep", "total")
        val_s = _v(a, "val", "score")
        lines.append(
            f"| {name} | {_f(g)} | {_f(p)} | {_f(m)} | {_f(comp)} | {flag} | {chain_s} | {deep_s} | {val_s} |"
        )
    lines.append("")

    # 三高达标 / 综合 top 标的（卡脖子抓手落脚点）
    high = [a for a in aligned if (a.get("three_high") or {}).get("three_high")]
    top_comp = [a for a in aligned if (a.get("three_high") or {}).get("composite") is not None][:5]
    lines.append("#### 卡脖子抓手标的（三高优先）")
    if high:
        for a in high:
            th = a.get("three_high") or {}
            lines.append(f"- **{a.get('name')}**：三高综合 {th.get('composite')} "
                         f"（增长{th.get('growth')}/利润{th.get('profit')}/围墙{th.get('moat')}）")
    elif top_comp:
        lines.append("> 无严格三高达标标的，以下为三高综合 top 5：")
        for a in top_comp:
            th = a.get("three_high") or {}
            miss = th.get("missing") or []
            note = f"（缺：{','.join(miss)}）" if miss else ""
            lines.append(f"- {a.get('name')}：三高综合 {th.get('composite')}{note}")
    else:
        lines.append("- （无可用三高数据）")
    lines.append("")
    return lines


def _f(v) -> str:
    return "—" if v is None else str(v)


def render_report(result: dict) -> str:
    sectors = result.get("sectors", [])
    title = "、".join(sectors) or "自动选板块"
    lines = [
        f"# 431 中国特色价值投资报告 — {title}",
        "",
        "> **框架**：4 层（宏观->市场->行业->公司）+ 3 高（高增长/高利润/高围墙）+ 1 抓手（卡脖子）",
        f"> 模式：{result.get('mode','—')}　窗口：{result.get('days','—')} 天　TopN：{result.get('top_n','—')}　"
        f"生成：{result.get('run_time','—')}",
        "",
    ]
    lines += _render_macro(result.get("macro", {}))
    lines += _render_market(result.get("market", {}))
    lines += _render_industry(sectors, result.get("pick_reason", ""))
    lines.append("## 四、公司层 — 三高筛选 + 卡脖子抓手")
    lines.append("")
    for cr in result.get("company_results", []):
        lines += _render_company(cr)
    lines.append("---")
    lines.append("")
    lines.append("> 免责声明：本报告由公开资料整理，仅供参考，不构成投资建议。市场有风险，投资需谨慎。")
    return "\n".join(lines)
