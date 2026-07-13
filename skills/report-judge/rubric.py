"""report-judge rubric 定义（SPEC §6）。

6 个维度，每维 0-100，加权合成总分 -> A(>=85)/B(70-84)/C(55-69)/D(<55)。
LLM 给每维 score + reason + issues；总分由代码按 weight 确定性合成（不信任 LLM 的 total）。
"""

# 维度定义：key / 名称 / 检查点 / 权重。权重之和 = 100。
RUBRIC = [
    {
        "key": "coverage",
        "name": "候选覆盖",
        "check": "核心公司是否都在？有无明显遗漏？（对照板块核心公司清单）",
        "weight": 20,
    },
    {
        "key": "evidence",
        "name": "证据质量",
        "check": "evidence 编号是否支撑结论？有无空引/编造？数据降级时是否明确标注？",
        "weight": 20,
    },
    {
        "key": "consistency",
        "name": "视角一致性",
        "check": "多视角（chain/deep/val/cycle）分数/分类有无矛盾？矛盾是否被解释？单视角报告此项给满。",
        "weight": 15,
    },
    {
        "key": "depth",
        "name": "分析深度",
        "check": "有无具体数据（EPS/PE/市占率/订单/产能）？还是套话空泛？",
        "weight": 15,
    },
    {
        "key": "actionability",
        "name": "可操作性",
        "check": "结论是否明确（推荐/回避/观望/待跟踪）？还是模棱两可？",
        "weight": 15,
    },
    {
        "key": "risk",
        "name": "风险提示",
        "check": "有无提周期峰值/解禁/估值泡沫/竞争加剧/数据局限？",
        "weight": 15,
    },
]

# key -> weight 速查
WEIGHTS = {d["key"]: d["weight"] for d in RUBRIC}
# key -> name 速查
DIM_NAMES = {d["key"]: d["name"] for d in RUBRIC}

# 等级阈值
def grade_from_total(total: int) -> str:
    """总分 -> 等级 A/B/C/D。"""
    if total >= 85:
        return "A"
    if total >= 70:
        return "B"
    if total >= 55:
        return "C"
    return "D"


def compose_total(dimensions: list) -> int:
    """按 weight 确定性合成总分（0-100）。

    dimensions: [{key, score, ...}]。缺失维度按 0 计；分数 clamp 到 [0,100]。
    """
    if not dimensions:
        return 0
    total = 0.0
    for d in dimensions:
        key = d.get("key")
        weight = WEIGHTS.get(key, 0)
        score = d.get("score")
        if score is None:
            score = 0
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(100.0, score))
        total += score * weight / 100.0
    return int(round(total))


def rubric_text() -> str:
    """把 RUBRIC 渲染成给 LLM 看的文本块。"""
    lines = []
    for d in RUBRIC:
        lines.append(f"- {d['name']}（{d['key']}，权重 {d['weight']}）：{d['check']}")
    return "\n".join(lines)
