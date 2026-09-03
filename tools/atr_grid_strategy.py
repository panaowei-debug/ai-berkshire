#!/usr/bin/env python3
"""ATR 网格交易策略核心引擎 — 基于广发金工 NATR 网格规格。

核心函数：
    calc_atr, calc_mc, calc_natr, natr_to_weight
    detect_timing, apply_timing
    step_backtest, generate_signal
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


DEFAULT_PARAMS = {
    "atr_period": 20,
    "weight_pivot": 0.50,
    "timing_enabled": True,
    "timing_adjust": 0.10,
    "atr_trend_period": 5,
    "initial_cash": 100000,
    "max_weight": 1.0,
    "min_weight": 0.0,
}


def _sma(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def calc_tr(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def calc_atr_series(bars: list, period: int) -> list:
    """返回与 bars 等长的 ATR 列表，前 period 项为 None。"""
    trs = []
    for i, bar in enumerate(bars):
        if i == 0:
            trs.append(bar["high"] - bar["low"])
        else:
            trs.append(calc_tr(bar["high"], bar["low"], bars[i - 1]["close"]))
    atrs = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        atrs[i] = sum(trs[i - period + 1:i + 1]) / period
    return atrs


def calc_mc_series(bars: list, period: int) -> list:
    """价格中枢 MC = SMA(close, N)。"""
    closes = [b["close"] for b in bars]
    mcs = [None] * len(bars)
    for i in range(period - 1, len(bars)):
        mcs[i] = sum(closes[i - period + 1:i + 1]) / period
    return mcs


def calc_natr(close: float, mc: float, atr: float) -> Optional[float]:
    if atr is None or atr <= 0 or mc is None:
        return None
    return (close - mc) / atr


def natr_to_weight(natr: float, weight_pivot: float = 0.50) -> float:
    """表1：NATR → 指数配置权重。"""
    if natr >= 5:
        return weight_pivot - 0.50
    if natr >= 4:
        return weight_pivot - 0.40
    if natr >= 3:
        return weight_pivot - 0.30
    if natr >= 2:
        return weight_pivot - 0.20
    if natr >= 1:
        return weight_pivot - 0.10
    if natr > -1:
        return weight_pivot
    if natr > -2:
        return weight_pivot + 0.10
    if natr > -3:
        return weight_pivot + 0.20
    if natr > -4:
        return weight_pivot + 0.30
    if natr > -5:
        return weight_pivot + 0.40
    return weight_pivot + 0.50


def detect_timing(close: float, mc: float, atr: float, atr_history: list,
                  atr_trend_period: int = 5) -> str:
    """择时信号：BULLISH / BEARISH / NEUTRAL。"""
    if mc is None or atr is None or len(atr_history) < atr_trend_period:
        return "NEUTRAL"
    valid = [a for a in atr_history[-atr_trend_period:] if a is not None]
    if len(valid) < atr_trend_period:
        return "NEUTRAL"
    atr_ma = sum(valid) / len(valid)
    atr_down = atr < atr_ma
    if not atr_down:
        return "NEUTRAL"
    if close > mc:
        return "BULLISH"
    if close < mc:
        return "BEARISH"
    return "NEUTRAL"


def apply_timing(weight: float, timing: str, adjust: float = 0.10,
                 max_w: float = 1.0, min_w: float = 0.0) -> float:
    if timing == "BULLISH":
        return min(max_w, weight + adjust)
    if timing == "BEARISH":
        return max(min_w, weight - adjust)
    return weight


def compute_target_weight(bars: list, idx: int, params: dict) -> dict:
    """计算某日目标权重及中间指标。"""
    p = {**DEFAULT_PARAMS, **params}
    period = p["atr_period"]
    if idx < period:
        return {"weight": p["weight_pivot"], "natr": None, "timing": "NEUTRAL",
                "atr": None, "mc": None, "ready": False}

    atrs = calc_atr_series(bars, period)
    mcs = calc_mc_series(bars, period)
    atr = atrs[idx]
    mc = mcs[idx]
    close = bars[idx]["close"]
    natr = calc_natr(close, mc, atr)
    base_weight = natr_to_weight(natr, p["weight_pivot"]) if natr is not None else p["weight_pivot"]

    timing = "NEUTRAL"
    if p.get("timing_enabled", True):
        timing = detect_timing(close, mc, atr, atrs[:idx + 1], p["atr_trend_period"])
        weight = apply_timing(base_weight, timing, p["timing_adjust"],
                              p["max_weight"], p["min_weight"])
    else:
        weight = base_weight

    weight = max(p["min_weight"], min(p["max_weight"], weight))
    return {
        "weight": weight,
        "base_weight": base_weight,
        "natr": round(natr, 4) if natr is not None else None,
        "timing": timing,
        "atr": round(atr, 6) if atr else None,
        "mc": round(mc, 4) if mc else None,
        "close": close,
        "ready": True,
    }


def step_backtest(state: dict, bar: dict, target: dict) -> dict:
    """单日回测步进：按目标权重调仓。"""
    price = bar["close"]
    total = state["cash"] + state["position"] * price
    if total <= 0:
        return state

    target_value = total * target["weight"]
    current_value = state["position"] * price
    diff_value = target_value - current_value

    trades = []
    if abs(diff_value) > total * 0.001:  # 0.1% 最小调仓阈值
        if diff_value > 0:
            shares = diff_value / price
            cost = shares * price
            if cost <= state["cash"]:
                state["position"] += shares
                state["cash"] -= cost
                trades.append({"action": "BUY", "shares": shares, "price": price})
        else:
            shares = min(state["position"], abs(diff_value) / price)
            if shares > 0:
                state["position"] -= shares
                state["cash"] += shares * price
                trades.append({"action": "SELL", "shares": shares, "price": price})

    state["last_weight"] = target["weight"]
    state["last_natr"] = target.get("natr")
    state["last_timing"] = target.get("timing")
    state["total_value"] = state["cash"] + state["position"] * price
    state["trades"] = trades
    return state


def new_backtest_state(params: dict) -> dict:
    p = {**DEFAULT_PARAMS, **params}
    return {
        "cash": p["initial_cash"],
        "position": 0.0,
        "last_weight": p["weight_pivot"],
        "last_natr": None,
        "last_timing": "NEUTRAL",
        "total_value": p["initial_cash"],
        "trades": [],
    }


def generate_signal(code: str, bars: list, quote: dict, state: dict,
                    params: dict) -> dict:
    """实时信号：对比当前价与目标权重，输出 BUY/SELL/HOLD。"""
    p = {**DEFAULT_PARAMS, **params}
    if len(bars) < p["atr_period"]:
        return _hold_signal(code, quote, "数据不足，等待 K 线积累")

    idx = len(bars) - 1
    target = compute_target_weight(bars, idx, p)
    price = float(quote.get("price", bars[-1]["close"]))
    total = state.get("cash", p["initial_cash"]) + state.get("position", 0) * price
    current_weight = (state.get("position", 0) * price / total) if total > 0 else 0
    target_weight = target["weight"]
    diff = target_weight - current_weight

    natr = target.get("natr")
    grid_level = _natr_grid_level(natr) if natr is not None else None

    if diff > 0.02:
        action = "BUY"
        reason = f"目标权重 {target_weight:.0%} > 当前 {current_weight:.0%}，NATR={natr}"
    elif diff < -0.02:
        action = "SELL"
        reason = f"目标权重 {target_weight:.0%} < 当前 {current_weight:.0%}，NATR={natr}"
    else:
        action = "HOLD"
        reason = f"权重接近目标 {target_weight:.0%}，NATR={natr}"

    return {
        "code": code,
        "name": quote.get("name", code),
        "action": action,
        "price": price,
        "target_weight": round(target_weight, 4),
        "current_weight": round(current_weight, 4),
        "grid_level": grid_level,
        "natr": natr,
        "atr": target.get("atr"),
        "mc": target.get("mc"),
        "timing": target.get("timing"),
        "reason": reason,
    }


def _natr_grid_level(natr: float) -> int:
    """NATR 对应网格层级（负=买入侧，正=卖出侧）。"""
    if natr >= 5:
        return 5
    if natr >= 4:
        return 4
    if natr >= 3:
        return 3
    if natr >= 2:
        return 2
    if natr >= 1:
        return 1
    if natr > -1:
        return 0
    if natr > -2:
        return -1
    if natr > -3:
        return -2
    if natr > -4:
        return -3
    if natr > -5:
        return -4
    return -5


def _hold_signal(code, quote, reason):
    return {
        "code": code,
        "name": quote.get("name", code),
        "action": "HOLD",
        "price": float(quote.get("price", 0)),
        "target_weight": None,
        "current_weight": None,
        "grid_level": None,
        "natr": None,
        "atr": None,
        "mc": None,
        "timing": "NEUTRAL",
        "reason": reason,
    }


def calc_max_drawdown(equity_curve: list) -> float:
    """最大回撤（百分比）。"""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd * 100


def calc_total_return(initial: float, final: float) -> float:
    if initial <= 0:
        return 0.0
    return (final - initial) / initial * 100


def calc_annualized_return(initial: float, final: float, days: int) -> float:
    if initial <= 0 or days <= 0:
        return 0.0
    ratio = final / initial
    years = days / 365.25
    if years <= 0:
        return 0.0
    return (ratio ** (1 / years) - 1) * 100


def decimal_pct(value: float, places: int = 2) -> str:
    d = Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)
    return f"{d}%"
