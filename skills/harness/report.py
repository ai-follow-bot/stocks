"""harness 报告渲染（Markdown）"""

from chain_agent import config


def _disclaimer(text: str) -> str:
    return text + "\n\n---\n\n> **免责声明：** " + config.DISCLAIMER_TEXT


def render_report(result: dict) -> str:
    if "error" in result:
        return f"❌ 失败: {result['error']}"

    mode = result.get("mode", "chain")
    subject = result.get("subject", "")
    paths = result.get("paths", {})
    path_errors = result.get("path_errors", {})
    aligned = result.get("aligned", [])

    lines = [
        f"# {subject} 三视角交叉验证（harness）",
        f"\n*生成时间: {result.get('run_time')} | 路径: "
        + ("chain_agent(确定性) + deep-analyze(LLM深度) + valuation-lens(估值镜头)"
           if mode == "chain" else "deep-analyze(LLM深度) + valuation-lens(估值镜头)")
        + "*\n",
    ]

    # 路径状态
    status_parts = [f"{k}: {'❌' if failed else '✓'}" for k, failed in paths.items()]
    if status_parts:
        lines.append(f"> 路径状态: {' | '.join(status_parts)}\n")
    for k, err in path_errors.items():
        lines.append(f"> ⚠️ {k} 失败: {err[:120]}\n")

    # LLM 综合
    lines.append("## 1. LLM 综合判断\n")
    lines.append(result.get("synthesis", "") or "(无)")

    # 对齐表
    lines.append("\n## 2. 标的对齐表\n")
    if mode == "chain":
        lines.append("| 代码 | 名称 | chain(score/role) | deep(total) | val(score/role) | 一致性 |")
        lines.append("|------|------|------|------|------|------|")
        for e in aligned:
            c = e.get("chain") or {}
            d = e.get("deep") or {}
            v = e.get("val") or {}
            lines.append(
                f"| {e['code']} | {e['name']} | {c.get('score','-')}({c.get('role','-')}) | "
                f"{d.get('total','-')} | {v.get('score','-')}({v.get('role','-')}) | "
                f"{e['consistency']['label']} |"
            )
    else:
        lines.append("| 代码 | 名称 | deep(total) | val(score/role) | 一致性 |")
        lines.append("|------|------|------|------|------|")
        for e in aligned:
            d = e.get("deep") or {}
            v = e.get("val") or {}
            lines.append(
                f"| {e['code']} | {e['name']} | {d.get('total','-')} | "
                f"{v.get('score','-')}({v.get('role','-')}) | {e['consistency']['label']} |"
            )

    # 方法论说明
    lines.append("\n## 3. 方法论说明\n")
    lines.append(
        "- **三视角共振**（chain/deep/val 都≥70）= 高置信机会，三路径独立判断一致。\n"
        "- **显著分歧**（极差>30）= 单一视角盲区或机会，标注哪路径高/低，需深究。\n"
        "- **一致偏弱**（都<50）= 回避。\n"
        "- **仅单路径覆盖** = 信号弱，需补证。\n"
        "- 分数口径：chain 0-100（规则）/ deep total_score 0-100（供需30+国产替代30+业绩兑现40）/"
        " val valuation_score 0-100（稀缺+前瞻+供需，PE 方向约束）。"
    )

    return _disclaimer("\n".join(lines))
