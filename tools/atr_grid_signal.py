#!/usr/bin/env python3
"""ATR 网格策略实时/模拟信号 CLI。

用法：
    python3 tools/atr_grid_signal.py --watchlist data/atr_grid/watchlist.json
    python3 tools/atr_grid_signal.py --output data/atr_grid/signals/latest.json
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from ashare_kline import fetch_with_cache, load_cached
from atr_grid_strategy import DEFAULT_PARAMS, generate_signal, new_backtest_state

_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "atr_grid", "state")
_TIMEOUT = 15


def _curl(url):
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("gbk")


def _qq_code(code: str) -> str:
    code = code.strip().replace(".SH", "").replace(".SZ", "")
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    return f"sz{code}"


def fetch_quote(code: str) -> dict:
    raw = _curl(f"https://qt.gtimg.cn/q={_qq_code(code)}")
    start = raw.find('"')
    end = raw.rfind('"')
    if start < 0:
        return {"code": code, "name": code, "price": "0"}
    fields = raw[start + 1:end].split("~")
    if len(fields) < 50:
        return {"code": code, "name": code, "price": "0"}
    return {
        "name": fields[1],
        "code": fields[2],
        "price": fields[3],
        "prev_close": fields[4],
        "open": fields[5],
        "high": fields[33] if len(fields) > 33 else fields[3],
        "low": fields[34] if len(fields) > 34 else fields[3],
    }


def load_state(code: str, params: dict) -> dict:
    os.makedirs(_STATE_DIR, exist_ok=True)
    path = os.path.join(_STATE_DIR, f"{code}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return new_backtest_state(params)


def save_state(code: str, state: dict):
    os.makedirs(_STATE_DIR, exist_ok=True)
    path = os.path.join(_STATE_DIR, f"{code}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_bars(code: str) -> list:
    cached = load_cached(code)
    if cached and len(cached) >= 30:
        return cached
    return fetch_with_cache(code, start="20180101")


def scan_watchlist(watchlist_path: str) -> dict:
    with open(watchlist_path, encoding="utf-8") as f:
        wl = json.load(f)

    params = wl.get("params", DEFAULT_PARAMS)
    signals = []
    tz = timezone(timedelta(hours=8))

    for i, code in enumerate(wl["etfs"]):
        if i > 0:
            time.sleep(2)
        name = wl.get("names", {}).get(code, code)
        try:
            bars = get_bars(code)
            quote = fetch_quote(code)
            state = load_state(code, params)
            sig = generate_signal(code, bars, quote, state, params)
            signals.append(sig)
        except Exception as e:
            signals.append({
                "code": code,
                "name": name,
                "action": "HOLD",
                "price": 0,
                "target_weight": None,
                "current_weight": None,
                "grid_level": None,
                "natr": None,
                "atr": None,
                "mc": None,
                "timing": "NEUTRAL",
                "reason": f"信号生成失败: {e}",
            })

    return {
        "generated_at": datetime.now(tz).isoformat(),
        "signals": signals,
    }


def print_signals(output: dict):
    print(f"\n{'=' * 60}")
    print(f"  ATR 网格策略信号  {output['generated_at']}")
    print(f"{'=' * 60}")
    for s in output["signals"]:
        action_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(s["action"], "⚪")
        print(f"\n  {action_icon} {s['name']} ({s['code']})  {s['action']}")
        print(f"     价格: {s['price']}")
        if s.get("target_weight") is not None:
            print(f"     目标权重: {s['target_weight']:.0%}  当前: {s.get('current_weight', 0):.0%}")
        if s.get("natr") is not None:
            print(f"     NATR: {s['natr']}  网格层级: {s.get('grid_level')}  择时: {s.get('timing')}")
        print(f"     {s['reason']}")


def main():
    parser = argparse.ArgumentParser(description="ATR 网格策略信号")
    parser.add_argument("--watchlist", default="data/atr_grid/watchlist.json")
    parser.add_argument("--output", help="JSON 输出路径")
    args = parser.parse_args()

    wl_path = args.watchlist
    if not os.path.isabs(wl_path):
        wl_path = os.path.join(os.path.dirname(__file__), "..", wl_path)

    output = scan_watchlist(wl_path)
    print_signals(output)

    if args.output:
        out_path = args.output
        if not os.path.isabs(out_path):
            out_path = os.path.join(os.path.dirname(__file__), "..", out_path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\n  📄 JSON 已写入: {out_path}")

        # 校验 JSON 可解析
        with open(out_path, encoding="utf-8") as f:
            json.loads(f.read())
        print("  ✅ JSON 校验通过")


if __name__ == "__main__":
    main()
