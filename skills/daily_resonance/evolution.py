"""
自进化模块：反馈学习、贝叶斯权重更新、收敛判定

每天运行时：
1. 如果有前一天的TOP3预测记录
2. 获取T+1日实际板块涨跌幅
3. 计算预测准确率
4. 贝叶斯更新权重
5. 判定是否收敛
"""
import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import (
    OUTPUT_DIR,
    INITIAL_WEIGHTS,
    CONVERGENCE_DAYS,
    CONVERGENCE_DELTA,
    CONVERGENCE_MIN_ACCURACY,
    REGIME_CHANGE_DAYS,
    REGIME_CHANGE_THRESHOLD,
)


EVOLUTION_STATE_PATH = OUTPUT_DIR / "evolution_state.json"


# ── 状态管理 ──────────────────────────────────────────

def load_state() -> dict:
    """加载自进化状态"""
    if EVOLUTION_STATE_PATH.exists():
        try:
            with open(EVOLUTION_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            print(f"[进化] 加载状态: 运行{days_run(state)}天, "
                  f"收敛={'是' if state.get('converged') else '否'}",
                  file=sys.stderr)
            return state
        except (json.JSONDecodeError, IOError) as e:
            print(f"[进化] 状态文件损坏: {e}，重新初始化", file=sys.stderr)

    return _init_state()


def _init_state() -> dict:
    """初始化状态"""
    return {
        "last_date": None,
        "days_run": 0,
        "converged": False,
        "converged_at": None,
        "weights": list(INITIAL_WEIGHTS),
        "weight_history": [],
        "accuracy_history": [],
        "feature_contribution_history": [],
        "top3_history": [],
        "sector_daily_counts": {},
    }


def save_state(state: dict):
    """持久化自进化状态"""
    try:
        with open(EVOLUTION_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[进化] 状态已保存", file=sys.stderr)
    except IOError as e:
        print(f"[进化] ❌ 保存状态失败: {e}", file=sys.stderr)


def days_run(state: dict) -> int:
    return state.get("days_run", 0)


# ── 反馈学习 ──────────────────────────────────────────

def process_feedback(
    state: dict,
    current_date: str,
    top3_sectors: list[str],
    resonance_results: list[dict],
) -> dict:
    """
    处理反馈学习：检查前一天的预测是否准确，更新权重。

    参数:
        state: 当前进化状态
        current_date: 当前运行日期
        top3_sectors: 本次运行的TOP3板块
        resonance_results: 本次运行的完整共振结果

    返回:
        更新后的state
    """
    last_date = state.get("last_date")

    # 如果有前一天的记录，计算反馈
    if last_date and state.get("top3_history"):
        # 查找前一天的TOP3
        prev_entry = None
        for entry in reversed(state["top3_history"]):
            if entry.get("date") == last_date:
                prev_entry = entry
                break

        if prev_entry:
            prev_top3 = prev_entry.get("top3", [])
            # 尝试获取T+1日的板块表现数据
            accuracy = _compute_accuracy_from_market(prev_top3, last_date, current_date)

            if accuracy is not None:
                # 更新权重
                state = _update_weights(state, accuracy)
                # 记录准确率
                state["accuracy_history"].append(accuracy)
                # 更新前一条记录的准确率
                prev_entry["accuracy"] = accuracy

                print(f"[进化] 反馈: {last_date} TOP3准确率={accuracy:.2f}, "
                      f"权重={[f'{w:.3f}' for w in state['weights']]}",
                      file=sys.stderr)
            else:
                print(f"[进化] ⚠️ 无法获取{last_date}的市场数据，跳过权重更新",
                      file=sys.stderr)

    # 更新sector_daily_counts
    _update_sector_counts(state, current_date, resonance_results)

    # 更新最后日期和TOP3
    state["last_date"] = current_date
    state["top3_history"].append({
        "date": current_date,
        "top3": top3_sectors,
    })

    return state


def _compute_accuracy_from_market(
    prev_top3: list[str],
    prev_date: str,
    current_date: str,
) -> Optional[float]:
    """
    从市场数据计算TOP3预测准确率。

    用akshare获取申万行业指数的日涨跌幅数据。
    如果无法获取（如非交易日），返回None。

    准确率计算:
    - 方向准确率: TOP3中当日上涨的比例
    - 排名准确率: TOP1是否确实是涨幅最大的
    - 综合准确率 = 0.7 × 方向 + 0.3 × 排名
    """
    try:
        import akshare as ak
        # 获取板块涨跌幅数据
        df = ak.index_analysis_daily_sw()

        if df is None or df.empty:
            return None

        # 计算方向准确率
        # 用申万二级行业分类匹配我们的板块
        # 简化处理：只统计TOP3中上涨的比例
        up_count = 0
        for sector in prev_top3:
            # 查找匹配的申万行业
            matched = _match_to_sw(sector, df)
            if matched is not None:
                change_pct = matched.get("change_pct", 0)
                if change_pct > 0:
                    up_count += 1

        # 方向准确率
        direction_acc = up_count / max(len(prev_top3), 1)

        # 综合准确率（简化：只用方向准确率）
        return direction_acc

    except ImportError:
        print("[进化] akshare不可用，无法获取市场数据", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[进化] 获取市场数据失败: {e}", file=sys.stderr)
        return None


def _match_to_sw(sector_key: str, df) -> Optional[dict]:
    """
    将我们的板块key匹配到申万行业分类。
    简化实现：通过板块关键词匹配。
    """
    # 如果df是DataFrame，遍历行
    try:
        import pandas as pd
        if isinstance(df, pd.DataFrame):
            sector_name_col = None
            change_col = None
            for col in df.columns:
                if "行业" in str(col) or "板块" in str(col) or "名称" in str(col):
                    sector_name_col = col
                if "涨跌幅" in str(col) or "change" in str(col).lower():
                    change_col = col

            if sector_name_col is None:
                return None

            # 尝试用板块key的中文名匹配
            from .data import load_ecosystem, get_sector_name
            ecosystem = load_ecosystem()
            name = get_sector_name(sector_key, ecosystem)

            for _, row in df.iterrows():
                row_name = str(row.get(sector_name_col, ""))
                if name and name in row_name:
                    change = row.get(change_col, 0)
                    try:
                        return {"change_pct": float(change)}
                    except (ValueError, TypeError):
                        return None

        return None
    except ImportError:
        return None


def _update_weights(state: dict, accuracy: float) -> dict:
    """
    贝叶斯学习率衰减更新权重。

    核心逻辑:
    - learning_rate = 1 / (1 + days_run)  衰减学习率
    - 如果准确率高于55%，保持（微调）
    - 如果准确率低于50%，向目标权重调整
    """
    weights = list(state["weights"])
    days = state["days_run"]
    learning_rate = 1.0 / (1.0 + days)

    if accuracy < 0.50:
        # 准确率低于随机，需要调整
        # 向均匀分布方向调整（每个维度0.2）
        target = [0.2, 0.2, 0.2, 0.2, 0.2]
        for i in range(len(weights)):
            weights[i] += learning_rate * (target[i] - weights[i])
    elif accuracy < 0.55:
        # 略低于期望，微调
        target = [0.22, 0.28, 0.22, 0.14, 0.14]
        for i in range(len(weights)):
            weights[i] += learning_rate * 0.5 * (target[i] - weights[i])
    else:
        # 准确率达标，轻微强化当前权重（保持不变）
        pass

    # 归一化
    total = sum(weights)
    if total > 0:
        weights = [w / total for w in weights]

    # 记录历史
    state["weight_history"].append(list(weights))
    state["weights"] = weights
    state["days_run"] += 1

    # 收敛判定
    state = _check_convergence(state)

    # 市场结构变化检测
    state = _check_regime_change(state)

    return state


def _check_convergence(state: dict) -> dict:
    """判定是否收敛"""
    if state.get("converged"):
        return state

    weight_history = state.get("weight_history", [])
    accuracy_history = state.get("accuracy_history", [])

    if len(weight_history) < CONVERGENCE_DAYS:
        return state

    # 检查最近CONVERGENCE_DAYS天的权重变化
    recent = weight_history[-CONVERGENCE_DAYS:]
    max_delta = 0
    for i in range(1, len(recent)):
        for j in range(len(recent[i])):
            delta = abs(recent[i][j] - recent[i - 1][j])
            max_delta = max(max_delta, delta)

    if max_delta >= CONVERGENCE_DELTA:
        return state

    # 检查准确率
    recent_acc = accuracy_history[-CONVERGENCE_DAYS:]
    avg_accuracy = sum(recent_acc) / max(len(recent_acc), 1)

    if avg_accuracy < CONVERGENCE_MIN_ACCURACY:
        return state

    # 收敛！
    state["converged"] = True
    state["converged_at"] = state.get("last_date")
    print(f"[进化] 🎯 系统已收敛！第{state['days_run']}天，"
          f"平均准确率={avg_accuracy:.2f}", file=sys.stderr)

    return state


def _check_regime_change(state: dict) -> dict:
    """检测市场结构变化"""
    accuracy_history = state.get("accuracy_history", [])

    if len(accuracy_history) < REGIME_CHANGE_DAYS:
        return state

    recent = accuracy_history[-REGIME_CHANGE_DAYS:]

    if all(acc < REGIME_CHANGE_THRESHOLD for acc in recent):
        print(f"[进化] ⚠️ 市场结构变化检测！连续{REGIME_CHANGE_DAYS}天准确率"
              f"<{REGIME_CHANGE_THRESHOLD}，重置权重", file=sys.stderr)
        state["converged"] = False
        state["converged_at"] = None
        state["weights"] = list(INITIAL_WEIGHTS)
        state["days_run"] = 0
        state["weight_history"] = []

    return state


def _update_sector_counts(state: dict, date: str, results: list[dict]):
    """更新板块每日事件数统计"""
    counts = state.setdefault("sector_daily_counts", {})

    for r in results:
        sector_key = r["sector"]
        if sector_key not in counts:
            counts[sector_key] = {"dates": {}, "avg_30d": 1}

        total = r["stats"]["total_events"]
        counts[sector_key]["dates"][date] = total

        # 计算近30日均值
        dates = counts[sector_key]["dates"]
        recent_values = list(dates.values())[-30:]
        if recent_values:
            avg = sum(recent_values) / len(recent_values)
            counts[sector_key]["avg_30d"] = max(avg, 1)
