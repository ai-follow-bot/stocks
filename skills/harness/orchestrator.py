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


def _spawn(module: str, args: list, tmp_json: str) -> dict:
    """spawn python -m <module> <args> --json --out <tmp_json>，读 JSON 返回。失败返回 {error}。"""
    cmd = [sys.executable, "-m", module] + args + ["--json", "--out", tmp_json]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           cwd=str(config.OUTPUT_DIR.parent))
        if r.returncode != 0:
            return {"error": f"{module} exit {r.returncode}: {(r.stderr or '')[-300:]}"}
        with open(tmp_json, encoding="utf-8") as f:
            return json.load(f)
    except subprocess.TimeoutExpired:
        return {"error": f"{module} timeout (>600s)"}
    except Exception as e:
        return {"error": f"{module}: {e}"}


def _run_paths(paths: list) -> dict:
    """并发 spawn 多路径。paths=[(key, module, args)]。返回 {key: result_json}。"""
    results = {}
    with ThreadPoolExecutor(max_workers=max(2, len(paths))) as ex:
        futs = {}
        for key, module, args in paths:
            tmp = tempfile.NamedTemporaryFile(suffix=f"_harness_{key}.json", delete=False,
                                              dir=str(config.OUTPUT_DIR))
            tmp_path = tmp.name
            tmp.close()
            futs[ex.submit(_spawn, module, args, tmp_path)] = (key, tmp_path)
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


def run_harness_chain(sector: str, days: int = 14, top_n: int = 8) -> dict:
    print(f"[harness] === chain {sector} | 三路径并发 (days={days}, top_n={top_n}) ===", file=sys.stderr)
    paths = [
        ("chain", "chain_agent.agent", [sector, "--days", str(days), "--top-n", str(top_n)]),
        ("deep", "skills.deep-analyze", ["--chain", sector, "--days", str(days), "--top-n", str(top_n)]),
        ("val", "skills.valuation-lens", ["--chain", sector, "--days", str(days), "--top-n", str(top_n)]),
    ]
    raw = _run_paths(paths)
    for k, v in raw.items():
        print(f"[harness] {k}: {'失败' if 'error' in v else '完成'}", file=sys.stderr)
    aligned = align.align_chain_results(raw)
    synthesis = _llm_synthesize(sector, aligned, raw, mode="chain")
    return {
        "mode": "chain",
        "subject": sector,
        "run_time": datetime.now().isoformat(),
        "days": days,
        "paths": {k: ("error" in v) for k, v in raw.items()},
        "path_errors": {k: v["error"] for k, v in raw.items() if "error" in v},
        "aligned": aligned,
        "synthesis": synthesis,
    }


def run_harness_stock(stock: str, days: int = 14) -> dict:
    print(f"[harness] === stock {stock} | 两路径并发 (deep+val) ===", file=sys.stderr)
    paths = [
        ("deep", "skills.deep-analyze", ["--stock", stock, "--days", str(days)]),
        ("val", "skills.valuation-lens", ["--stock", stock, "--days", str(days)]),
    ]
    raw = _run_paths(paths)
    for k, v in raw.items():
        print(f"[harness] {k}: {'失败' if 'error' in v else '完成'}", file=sys.stderr)
    aligned = align.align_stock_results(raw)
    synthesis = _llm_synthesize(stock, aligned, raw, mode="stock")
    return {
        "mode": "stock",
        "subject": stock,
        "run_time": datetime.now().isoformat(),
        "days": days,
        "paths": {k: ("error" in v) for k, v in raw.items()},
        "path_errors": {k: v["error"] for k, v in raw.items() if "error" in v},
        "aligned": aligned,
        "synthesis": synthesis,
    }
