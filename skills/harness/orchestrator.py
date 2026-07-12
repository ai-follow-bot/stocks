"""harness 编排：并发 spawn 三路径（--chain）或两路径（--stock），收 JSON，对齐，LLM 综合。"""

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from chain_agent import config
from chain_agent.llm.client import get_llm_client

from . import align, prompts


def _spawn(module: str, args: list, tmp_json: str, timeout: int = 600) -> dict:
    """spawn python -m <module> <args> --json --out <tmp_json>，读 JSON 返回。失败返回 {error}。

    timeout 为该路径的墙钟上限（秒）。超时时 subprocess 会杀掉子进程，但
    TimeoutExpired.stdout/stderr 仍携带已捕获的部分输出 -> 取尾部写入 error 并打到
    stderr，便于诊断（否则超时即全丢，无任何线索）。
    """
    cmd = [sys.executable, "-m", module] + args + ["--json", "--out", tmp_json]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(config.OUTPUT_DIR.parent))
        if r.returncode != 0:
            return {"error": f"{module} exit {r.returncode}: {(r.stderr or '')[-300:]}"}
        with open(tmp_json, encoding="utf-8") as f:
            return json.load(f)
    except subprocess.TimeoutExpired as e:
        # 捕获已产生的输出尾部，避免超时即全丢（text=True 下通常为 str，个别版本返回 bytes）
        partial = e.stderr or e.stdout or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        tail = partial[-400:].strip()
        if tail:
            print(f"[harness] {module} 超时(>{timeout}s) 部分输出尾部:\n{tail}", file=sys.stderr)
        return {"error": f"{module} timeout (>{timeout}s)" + (f": {tail}" if tail else "")}
    except Exception as e:
        return {"error": f"{module}: {e}"}


def _run_paths(paths: list) -> dict:
    """并发 spawn 多路径。paths=[(key, module, args, timeout)]。返回 {key: result_json}。"""
    results = {}
    with ThreadPoolExecutor(max_workers=max(2, len(paths))) as ex:
        futs = {}
        for key, module, args, timeout in paths:
            tmp = tempfile.NamedTemporaryFile(suffix=f"_harness_{key}.json", delete=False,
                                              dir=str(config.OUTPUT_DIR))
            tmp_path = tmp.name
            tmp.close()
            futs[ex.submit(_spawn, module, args, tmp_path, timeout)] = (key, tmp_path)
        for fut in as_completed(futs):
            key, tmp_path = futs[fut]
            try:
                results[key] = fut.result()
            except Exception as e:
                results[key] = {"error": str(e)}
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    return results


def _llm_synthesize(subject: str, aligned: list, raw: dict, mode: str = "chain") -> str:
    client = get_llm_client()
    if client is None:
        return "(LLM 不可用，无综合判断；请看下方对齐表自行判断)"
    try:
        user = prompts.SYNTH_USER_TEMPLATE.format(
            subject=subject, mode=mode,
            aligned_text=align.render_aligned_text(aligned, mode=mode),
            paths_summary=align.render_paths_summary(raw, mode=mode),
        )
        return client.synthesize(prompts.SYNTH_SYSTEM, user)
    except Exception as e:
        return f"(LLM 综合失败: {e})"


def _path_timeout(key: str, default: int, override: int = None) -> int:
    """解析单路径超时：CLI --timeout 覆盖全部；否则 env HARNESS_<KEY>_TIMEOUT -> default。
    deep 配 kimi 实测 643–2225s，远超原先统一的 600s 上限，故按路径分级（chain600/deep1600/val1200）。"""
    if override and override > 0:
        return override
    env_val = os.environ.get(f"HARNESS_{key.upper()}_TIMEOUT")
    if env_val:
        try:
            return max(60, int(env_val))
        except ValueError:
            pass
    return default


def _extract_bottlenecks(deep_raw: dict) -> dict:
    """从 deep-analyze 原始输出提卡脖子环节，供上层（如 ce-value）做"卡脖子抓手"用。
    deep 顶层 bottleneck = {top_bottlenecks:[...], segments:[{name,bottleneck_score,is_bottleneck,...}]}。
    始终返回 {top_bottlenecks, segments} 结构（deep 失败/缺字段时为空列表），便于消费方直索引。"""
    empty = {"top_bottlenecks": [], "segments": []}
    if not isinstance(deep_raw, dict) or "error" in deep_raw:
        return empty
    bn = deep_raw.get("bottleneck")
    if not isinstance(bn, dict):
        return empty
    return {
        "top_bottlenecks": bn.get("top_bottlenecks", []) or [],
        "segments": [
            {"name": s.get("name"), "score": s.get("bottleneck_score"),
             "is_bottleneck": s.get("is_bottleneck")}
            for s in bn.get("segments", []) if isinstance(s, dict)
        ],
    }


def run_harness_chain(sector: str, days: int = 14, top_n: int = 8, timeout: int = None) -> dict:
    print(f"[harness] === chain {sector} | 四视角并发 (days={days}, top_n={top_n}) ===", file=sys.stderr)
    paths = [
        ("chain", "chain_agent.agent", [sector, "--days", str(days), "--top-n", str(top_n)],
         _path_timeout("chain", 600, timeout)),
        ("deep", "skills.deep-analyze", ["--chain", sector, "--days", str(days), "--top-n", str(top_n)],
         _path_timeout("deep", 1600, timeout)),
        ("val", "skills.valuation-lens", ["--chain", sector, "--days", str(days), "--top-n", str(top_n)],
         _path_timeout("val", 1200, timeout)),
        ("cycle", "skills.cycle-lens", ["--chain", sector, "--days", str(days), "--top-n", str(top_n)],
         _path_timeout("cycle", 1200, timeout)),
    ]
    raw = _run_paths(paths)
    for k, v in raw.items():
        print(f"[harness] {k}: {'失败' if 'error' in v else '完成'}", file=sys.stderr)
    aligned = align.align_chain_results(raw)
    synthesis = _llm_synthesize(sector, aligned, raw, mode="chain")
    deep_raw = raw.get("deep", {})
    return {
        "mode": "chain",
        "subject": sector,
        "run_time": datetime.now().isoformat(),
        "days": days,
        "paths": {k: ("error" in v) for k, v in raw.items()},
        "path_errors": {k: v["error"] for k, v in raw.items() if "error" in v},
        "aligned": aligned,
        "deep_bottlenecks": _extract_bottlenecks(deep_raw),
        "synthesis": synthesis,
    }


def run_harness_stock(stock: str, days: int = 14, timeout: int = None) -> dict:
    print(f"[harness] === stock {stock} | 两路径并发 (deep+val) ===", file=sys.stderr)
    paths = [
        ("deep", "skills.deep-analyze", ["--stock", stock, "--days", str(days)],
         _path_timeout("deep", 1600, timeout)),
        ("val", "skills.valuation-lens", ["--stock", stock, "--days", str(days)],
         _path_timeout("val", 1200, timeout)),
    ]
    raw = _run_paths(paths)
    for k, v in raw.items():
        print(f"[harness] {k}: {'失败' if 'error' in v else '完成'}", file=sys.stderr)
    aligned = align.align_stock_results(raw)
    synthesis = _llm_synthesize(stock, aligned, raw, mode="stock")
    deep_raw = raw.get("deep", {})
    return {
        "mode": "stock",
        "subject": stock,
        "run_time": datetime.now().isoformat(),
        "days": days,
        "paths": {k: ("error" in v) for k, v in raw.items()},
        "path_errors": {k: v["error"] for k, v in raw.items() if "error" in v},
        "aligned": aligned,
        "deep_bottlenecks": _extract_bottlenecks(deep_raw),
        "synthesis": synthesis,
    }
