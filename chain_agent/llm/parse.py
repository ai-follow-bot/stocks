"""LLM 输出 JSON 解析工具（provider-agnostic）。

从 LLM 文本中抠出 JSON，容忍 markdown 围栏、多 JSON 块、顶层为数组、
对象/数组内部的 `{`/`}`/`[`/`]` 干扰（字符级栈匹配 + 字符串引号/转义感知）。

供 skills/*-deep-analyze、skills/valuation-lens 等所有 LLM pipeline 复用，
避免每个 skill 各抄一份。
"""

import json
import re


def _strip_fences(text: str) -> str:
    """去掉 markdown 代码块围栏（保留块外文字）。"""
    if not text:
        return ""
    t = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    t = re.sub(r"\s*```$", "", t.strip())
    return t


def _find_first_json(t: str):
    """字符级栈匹配找首个完整的 {...} 或 [...]，返回 (candidate_str, end_idx) 或 None。

    跟踪字符串引号/转义，避免被对象/数组内部的花括号/方括号干扰。
    """
    i = 0
    n = len(t)
    while i < n:
        brace_obj = t.find("{", i)
        brace_arr = t.find("[", i)
        if brace_obj < 0 and brace_arr < 0:
            return None
        elif brace_obj < 0:
            brace, open_ch, close_ch = brace_arr, "[", "]"
        elif brace_arr < 0:
            brace, open_ch, close_ch = brace_obj, "{", "}"
        else:
            if brace_obj <= brace_arr:
                brace, open_ch, close_ch = brace_obj, "{", "}"
            else:
                brace, open_ch, close_ch = brace_arr, "[", "]"

        depth = 0
        in_str = False
        esc = False
        end = -1
        j = brace
        while j < n:
            ch = t[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            j += 1
        if end < 0:
            return None
        candidate = t[brace:end + 1]
        try:
            return json.loads(candidate), end
        except Exception:
            i = end + 1  # 这个块解析失败，从 end 之后找下一个
    return None


def json_from_llm(text: str):
    """从 LLM 输出中抠出 JSON。

    返回类型不固定：对象 → dict，数组 → list，无匹配 → None。
    下游调用方需 isinstance 判断类型。
    """
    if not text:
        return None
    t = _strip_fences(text)
    found = _find_first_json(t)
    return found[0] if found else None


def split_text_and_json(text: str):
    """从 LLM 输出中分离前置文本和 JSON 数据。

    返回 (preamble_text, json_data)。preamble 是 JSON 之前的前置文字（已清围栏），
    json_data 是首个成功解析的 JSON 对象/数组，无则 None。
    """
    if not text:
        return "", None
    t = _strip_fences(text)
    found = _find_first_json(t)
    if not found:
        return t.strip(), None
    data, end = found
    # 找到 JSON 起始位置（_find_first_json 内部从 i=0 开始，首个匹配的起点）
    # 重新定位起始 brace
    brace_obj = t.find("{")
    brace_arr = t.find("[")
    if brace_obj < 0 and brace_arr < 0:
        return t.strip(), data
    elif brace_obj < 0:
        start = brace_arr
    elif brace_arr < 0:
        start = brace_obj
    else:
        start = min(brace_obj, brace_arr)
    preamble = t[:start].strip()
    return preamble, data
