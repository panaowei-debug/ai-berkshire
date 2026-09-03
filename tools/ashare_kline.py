#!/usr/bin/env python3
"""A股 K 线数据工具 — 东方财富 push2his，零外部依赖。

用法：
    python3 tools/ashare_kline.py fetch 159937 --start 2023-01-01
    python3 tools/ashare_kline.py fetch 159937 --cache
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

_TIMEOUT = 30
_MAX_RETRIES = 4
_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "atr_grid", "prices")


def _curl(url):
    last_err = None
    for attempt in range(_MAX_RETRIES):
        if attempt > 0:
            time.sleep(2 ** attempt)
        result = subprocess.run(
            ["/usr/bin/curl", "-s", "--noproxy", "*",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             "-H", "Referer: https://quote.eastmoney.com/",
             url],
            capture_output=True, timeout=_TIMEOUT,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.decode("utf-8"))
        last_err = result.stderr.decode() or f"exit {result.returncode}"
    raise ConnectionError(f"请求失败 ({_MAX_RETRIES} 次): {url} — {last_err}")


def market_code(code: str) -> tuple:
    """返回 (market_id, clean_code)。1=SH, 0=SZ"""
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return "1", code
    return "0", code


def _fmt_date(s: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _qq_prefix(code: str) -> str:
    market, clean = market_code(code)
    return ("sh" if market == "1" else "sz") + clean


def fetch_kline_tencent(code: str, limit: int = 2000) -> list:
    """腾讯 K 线备用数据源。返回格式与 fetch_kline 一致。"""
    qq = _qq_prefix(code)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={qq},day,,,{limit},qfq"
    )
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"腾讯 K 线请求失败: {code}")
    data = json.loads(result.stdout.decode("utf-8"))
    raw = data.get("data", {}).get(qq, {}).get("day") or []
    rows = []
    for item in raw:
        rows.append({
            "date": item[0],
            "open": float(item[1]),
            "close": float(item[2]),
            "high": float(item[3]),
            "low": float(item[4]),
            "volume": float(item[5]),
            "amount": 0.0,
        })
    return rows


def fetch_kline(code: str, start: str = "20180101", end: str = None,
                klt: int = 101) -> list:
    """获取日 K 线。klt=101 日 K。

    返回: [{date, open, high, low, close, volume, amount}, ...]
    """
    if end is None:
        end = datetime.now().strftime("%Y%m%d")
    start_fmt = start.replace("-", "")
    end_fmt = end.replace("-", "")

    market, clean = market_code(code)
    params = {
        "secid": f"{market}.{clean}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": str(klt),
        "fqt": "1",
        "beg": start_fmt,
        "end": end_fmt,
    }
    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urlencode(params)}"
    try:
        data = _curl(url)
    except ConnectionError:
        rows = fetch_kline_tencent(code)
        if start_fmt or end_fmt:
            rows = [r for r in rows if r["date"] >= _fmt_date(start_fmt)
                    and r["date"] <= _fmt_date(end_fmt)]
        return rows
    klines = data.get("data", {}).get("klines") or []
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        rows.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]) if len(parts) > 6 else 0.0,
        })
    return rows


def cache_path(code: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{code}.json")


def load_cached(code: str) -> list:
    path = cache_path(code)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("bars", [])


def save_cache(code: str, bars: list, name: str = ""):
    path = cache_path(code)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "code": code,
            "name": name,
            "updated": datetime.now().isoformat(),
            "bars": bars,
        }, f, ensure_ascii=False, indent=2)


def fetch_with_cache(code: str, start: str = "20180101", end: str = None) -> list:
    """拉取 K 线并写入本地缓存。"""
    bars = fetch_kline(code, start=start, end=end)
    if bars:
        save_cache(code, bars)
    return bars


def cmd_fetch(code: str, start: str, end: str, use_cache: bool):
    if use_cache:
        cached = load_cached(code)
        if cached:
            print(f"  缓存命中: {code} ({len(cached)} 条)")
            return cached
    print(f"  拉取 K 线: {code} {start} ~ {end or 'today'}")
    bars = fetch_with_cache(code, start=start, end=end)
    if not bars:
        print(f"  ❌ 无数据: {code}")
        return []
    print(f"  ✅ {len(bars)} 条, {bars[0]['date']} ~ {bars[-1]['date']}")
    return bars


def main():
    parser = argparse.ArgumentParser(description="A股 K 线数据")
    parser.add_argument("action", choices=["fetch"])
    parser.add_argument("code", help="股票/ETF 代码")
    parser.add_argument("--start", default="20180101")
    parser.add_argument("--end", default=None)
    parser.add_argument("--cache", action="store_true", help="使用/写入本地缓存")
    args = parser.parse_args()

    if args.action == "fetch":
        cmd_fetch(args.code, args.start, args.end, args.cache)


if __name__ == "__main__":
    main()
