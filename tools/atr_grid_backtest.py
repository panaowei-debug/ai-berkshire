#!/usr/bin/env python3
"""ATR 网格策略回测 CLI。

用法：
    python3 tools/atr_grid_backtest.py --code 159937 --start 2023-01-01
    python3 tools/atr_grid_backtest.py --watchlist data/atr_grid/watchlist.json
    python3 tools/atr_grid_backtest.py --watchlist data/atr_grid/watchlist.json --report
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from ashare_kline import fetch_with_cache, load_cached
from atr_grid_strategy import (
    DEFAULT_PARAMS,
    calc_annualized_return,
    calc_max_drawdown,
    calc_total_return,
    compute_target_weight,
    decimal_pct,
    new_backtest_state,
    step_backtest,
)


def load_watchlist(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_bars(code: str, start: str, end: str) -> list:
    cached = load_cached(code)
    if cached and cached[0]["date"] <= start.replace("-", "")[:10]:
        bars = [b for b in cached if b["date"] >= start and (not end or b["date"] <= end)]
        if bars:
            return bars
    return fetch_with_cache(code, start=start.replace("-", ""), end=end.replace("-", "") if end else None)


def run_backtest(code: str, name: str, bars: list, params: dict) -> dict:
    period = params.get("atr_period", DEFAULT_PARAMS["atr_period"])
    state = new_backtest_state(params)
    equity_curve = []
    trade_count = 0
    buy_count = 0
    sell_count = 0
    daily_returns = []

    for i in range(len(bars)):
        if i < period:
            equity_curve.append(state["cash"])
            continue
        target = compute_target_weight(bars, i, params)
        prev_value = state.get("total_value", state["cash"])
        state = step_backtest(state, bars[i], target)
        equity_curve.append(state["total_value"])
        for t in state.get("trades", []):
            trade_count += 1
            if t["action"] == "BUY":
                buy_count += 1
            else:
                sell_count += 1
        if prev_value > 0:
            daily_returns.append((state["total_value"] - prev_value) / prev_value)

    initial = params.get("initial_cash", DEFAULT_PARAMS["initial_cash"])
    final = equity_curve[-1] if equity_curve else initial
    days = len(bars) - period
    max_dd = calc_max_drawdown(equity_curve[period:])
    total_ret = calc_total_return(initial, final)
    ann_ret = calc_annualized_return(initial, final, max(days, 1))

    # 基准：买入持有
    if len(bars) > period:
        bh_start = bars[period]["close"]
        bh_end = bars[-1]["close"]
        benchmark_ret = calc_total_return(bh_start, bh_end)
    else:
        benchmark_ret = 0.0

    win_days = sum(1 for r in daily_returns if r > 0)
    win_rate = win_days / len(daily_returns) * 100 if daily_returns else 0

    return {
        "code": code,
        "name": name,
        "start": bars[period]["date"] if len(bars) > period else bars[0]["date"],
        "end": bars[-1]["date"],
        "days": days,
        "initial": initial,
        "final": round(final, 2),
        "total_return_pct": round(total_ret, 2),
        "annualized_return_pct": round(ann_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_return_pct": round(benchmark_ret, 2),
        "trade_count": trade_count,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "win_rate_pct": round(win_rate, 1),
        "final_position": round(state["position"], 2),
        "final_weight": round(state.get("last_weight", 0), 4),
        "final_natr": state.get("last_natr"),
    }


def print_result(r: dict):
    print(f"\n{'=' * 60}")
    print(f"  {r['name']} ({r['code']})  ATR网格回测")
    print(f"{'=' * 60}")
    print(f"  区间:       {r['start']} ~ {r['end']} ({r['days']} 交易日)")
    print(f"  初始资金:   {r['initial']:,.0f}")
    print(f"  期末净值:   {r['final']:,.2f}")
    print(f"  总收益率:   {decimal_pct(r['total_return_pct'])}")
    print(f"  年化收益:   {decimal_pct(r['annualized_return_pct'])}")
    print(f"  最大回撤:   {decimal_pct(r['max_drawdown_pct'])}")
    print(f"  基准持有:   {decimal_pct(r['benchmark_return_pct'])}")
    print(f"  调仓次数:   {r['trade_count']} (买{r['buy_count']}/卖{r['sell_count']})")
    print(f"  日胜率:     {decimal_pct(r['win_rate_pct'])}")
    print(f"  期末持仓:   {r['final_position']} 股, 权重 {r['final_weight']:.0%}")
    if r.get("final_natr") is not None:
        print(f"  期末 NATR:  {r['final_natr']}")


def write_report(results: list, path: str):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# ATR 网格策略回测报告",
        f"",
        f"> 数据截止：{today}",
        f"> 策略来源：广发金工 ATR ETF 网格交易策略",
        f"",
    ]
    for r in results:
        lines += [
            f"## {r['name']} ({r['code']})",
            f"",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 回测区间 | {r['start']} ~ {r['end']} |",
            f"| 总收益率 | {r['total_return_pct']:.2f}% |",
            f"| 年化收益 | {r['annualized_return_pct']:.2f}% |",
            f"| 最大回撤 | {r['max_drawdown_pct']:.2f}% |",
            f"| 买入持有 | {r['benchmark_return_pct']:.2f}% |",
            f"| 调仓次数 | {r['trade_count']} |",
            f"| 日胜率 | {r['win_rate_pct']:.1f}% |",
            f"",
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  📄 报告已写入: {path}")


def main():
    parser = argparse.ArgumentParser(description="ATR 网格策略回测")
    parser.add_argument("--code", help="单标的代码")
    parser.add_argument("--watchlist", help="watchlist.json 路径")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--report", action="store_true", help="输出 Markdown 报告")
    args = parser.parse_args()

    if not args.code and not args.watchlist:
        parser.error("请指定 --code 或 --watchlist")

    if args.watchlist:
        wl = load_watchlist(args.watchlist)
        codes = wl["etfs"]
        names = wl.get("names", {})
        params = wl.get("params", DEFAULT_PARAMS)
    else:
        codes = [args.code]
        names = {}
        params = DEFAULT_PARAMS

    results = []
    for i, code in enumerate(codes):
        if i > 0:
            time.sleep(5)
        name = names.get(code, code)
        print(f"\n拉取 {name} ({code}) ...")
        try:
            bars = get_bars(code, args.start, args.end or datetime.now().strftime("%Y-%m-%d"))
            if not bars:
                print(f"  ❌ 跳过 {code}: 无 K 线数据")
                continue
            r = run_backtest(code, name, bars, params)
            print_result(r)
            results.append(r)
        except Exception as e:
            print(f"  ❌ {code} 回测失败: {e}")

    if args.report and results:
        today = datetime.now().strftime("%Y%m%d")
        report_path = os.path.join(
            os.path.dirname(__file__), "..", "reports", f"ATR网格-backtest-{today}.md"
        )
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        write_report(results, report_path)

    if not results:
        sys.exit(1)


if __name__ == "__main__":
    main()
