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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import (
    OUTPUT_DIR,
    DATA_DIR,
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
    用个股行情计算TOP3预测准确率。

    对每个TOP3板块，取 leader 股票（来自 sector_overflow_config.json），
    查询个股日涨跌幅。如果超过半数 leader 上涨，该板块计为"上涨"。
    方向准确率 = 上涨板块数 / 有数据板块数。

    如果无法获取行情数据（非交易日等），返回None。
    """
    # chain_agent.config 会调用 clear_proxy_env() 确保 akshare 直连
    # 但二次导入不生效，显式清代理
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "all_proxy", "ALL_PROXY"]:
        os.environ.pop(k, None)
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

    try:
        import akshare as ak
    except ImportError:
        print("[进化] akshare不可用，无法获取市场数据", file=sys.stderr)
        return None

    # 加载板块龙头股配置
    overflow_path = DATA_DIR / "sector_overflow_config.json"
    if not overflow_path.exists():
        print(f"[进化] 未找到板块配置: {overflow_path}", file=sys.stderr)
        return None

    with open(overflow_path, "r", encoding="utf-8") as f:
        overflow_config = json.load(f)

    # ── 板块key映射：ecosystem (underscore) → overflow_config (混合命名) ──
    # overflow_config 的 key 与 ecosystem 不完全一致，需要手动映射
    SECTOR_TO_OVERFLOW = {
        "optical_module": "optical-module",
        "pcb": "pcb",
        "liquid_cooling": "liquid-cooling",
        "cooling_components": "liquid-cooling",
        "storage": "storage",
        "ocs": "ocs",
        "mlcc": "mlcc",
        "cpo": "CPO",
        "cpu": "CPU",
        "hbm": "HBM",
        "hbn": "HBN",
        "npo": "NPO",
        "tgv": "TGV",
        "copper_foil": "高端铜箔",
        "hbm_components": "HBM",
        "optical_chip": "光芯片",
        "功率半导体": "功率半导体",
        "机器人": "机器人",
        "物理AI": "物理AI",
        "特种集成电路": "特种集成电路",
        "玻璃基板": "玻璃基板",
        "薄膜铌酸锂": "薄膜铌酸锂",
        "高端铜箔": "高端铜箔",
        "半导体材料": "半导体材料",
        "半导体设备": "半导体设备",
    }

    # 遍历每个TOP3板块，用 leader 股票涨跌幅判断板块方向
    up_count = 0
    sector_count = 0

    for sk in prev_top3:
        hk = SECTOR_TO_OVERFLOW.get(sk)
        if not hk:
            hk = sk.replace("_", "-")
        sector_config = overflow_config.get(hk)
        if not sector_config:
            continue

        leaders = sector_config.get("leaders", [])
        codes = []
        for l in leaders:
            if isinstance(l, dict):
                code = l.get("code", "")
            else:
                code = str(l)
            if code:
                codes.append(code)

        if not codes:
            continue

        # 查询每个 leader 股票的日涨跌幅（带重试）
        up_stocks = 0
        total_stocks = 0

        # akshare 的 start_date/end_date 需要 YYYYMMDD 格式（无连字符）
        ak_date = prev_date.replace("-", "")

        for code in codes:
            # 重试最多3次，应对 RemoteDisconnected 等瞬时网络波动
            df = None
            for attempt in range(3):
                try:
                    df = ak.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=ak_date,
                        end_date=ak_date,
                        adjust="qfq",
                    )
                    if df is not None and not df.empty:
                        break
                except Exception:
                    if attempt < 2:
                        time.sleep(1)
                    continue

            if df is not None and not df.empty:
                total_stocks += 1
                change_pct = df.iloc[-1].get("涨跌幅", 0)
                if isinstance(change_pct, (int, float)) and change_pct > 0:
                    up_stocks += 1

        if total_stocks > 0:
            sector_count += 1
            if up_stocks / total_stocks > 0.5:
                up_count += 1

    if sector_count == 0:
        return None

    return up_count / sector_count


def _match_to_sw(sector_key: str, df) -> Optional[dict]:
    """已废弃 — 改用个股行情计算准确率"""
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
