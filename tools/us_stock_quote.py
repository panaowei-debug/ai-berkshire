#!/usr/bin/env python3
"""美股行情工具 — 盘中 Finnhub 实时，收盘后 TradingView。

规则（美东常规交易时段 9:30–16:00，周一至周五）：
  - 盘中：Finnhub /quote API（实时涨跌幅）
  - 收盘后 / 盘前：TradingView 页面（上一交易日收盘数据）

北京时间对照（夏令时 EDT）：约 21:30 – 次日 04:00
北京时间对照（冬令时 EST）：约 22:30 – 次日 05:00

用法：
    export FINNHUB_API_KEY=your_key   # https://finnhub.io 免费注册
    python3 tools/us_stock_quote.py MU
    python3 tools/us_stock_quote.py MU --source finnhub   # 强制 Finnhub
    python3 tools/us_stock_quote.py MU --source tradingview

需要 Python >= 3.9，零外部依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

_TIMEOUT = 15
_ET = ZoneInfo("America/New_York")
_BJ = ZoneInfo("Asia/Shanghai")
_FINNHUB_BASE = "https://finnhub.io/api/v1"


def _curl(url: str) -> str:
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True,
        timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    return result.stdout.decode("utf-8", errors="replace")


def is_us_regular_session(now: datetime | None = None) -> bool:
    """美东常规交易时段：周一至周五 9:30–16:00。"""
    now = now or datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return time(9, 30) <= now.time() < time(16, 0)


def _bj_window_hint() -> str:
    """返回当前季节对应的北京时间交易窗口说明。"""
    today_et = datetime.now(_ET).date()
    open_bj = datetime.combine(today_et, time(9, 30), tzinfo=_ET).astimezone(_BJ)
    close_bj = datetime.combine(today_et, time(16, 0), tzinfo=_ET).astimezone(_BJ)
    close_label = "次日" if close_bj.date() != open_bj.date() else "当日"
    return f"北京时间约 {open_bj.strftime('%H:%M')} – {close_bj.strftime('%H:%M')}（{close_label}）"


def fetch_finnhub(symbol: str) -> dict:
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 FINNHUB_API_KEY 环境变量")

    url = f"{_FINNHUB_BASE}/quote?symbol={symbol.upper()}&token={api_key}"
    data = json.loads(_curl(url))
    if "error" in data:
        raise RuntimeError(data["error"])

    current = data.get("c", 0)
    if not current:
        raise RuntimeError(f"Finnhub 无有效报价: {data}")

    return {
        "symbol": symbol.upper(),
        "source": "finnhub",
        "price": current,
        "change": data.get("d"),
        "change_pct": data.get("dp"),
        "prev_close": data.get("pc"),
        "high": data.get("h"),
        "low": data.get("l"),
        "open": data.get("o"),
        "timestamp": data.get("t"),
        "session": "regular" if is_us_regular_session() else "extended_or_closed",
        "note": "Finnhub 实时报价",
    }


def fetch_tradingview(symbol: str) -> dict:
    url = f"https://cn.tradingview.com/symbols/NASDAQ-{symbol.upper()}/"
    html = _curl(url)

    price = None
    change_pct = None
    change = None

    # FAQ: "MU当前价格为959.48 USD — 在过去24小时内下跌了−1.17%"
    faq = re.search(
        r"当前价格为\s*([\d,.]+)\s*USD\s*—\s*在过去24小时内(?:上涨|下跌)了\s*([−\-+]?[\d,.]+)%",
        html,
    )
    if faq:
        price = float(faq.group(1).replace(",", ""))
        change_pct = float(faq.group(2).replace("−", "-").replace(",", ""))

    # 顶部区间：1天−1.17%
    if change_pct is None:
        m = re.search(r"1天([−\-+]?[\d,.]+)%", html)
        if m:
            change_pct = float(m.group(1).replace("−", "-").replace(",", ""))

    # 涨跌额：+16.00 在部分页面
    if price and change_pct is not None and change is None:
        prev = price / (1 + change_pct / 100) if change_pct != -100 else None
        if prev:
            change = round(price - prev, 2)

    market_closed = "休市" in html or "Market closed" in html

    if price is None:
        raise RuntimeError("无法从 TradingView 页面解析价格")

    return {
        "symbol": symbol.upper(),
        "source": "tradingview",
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "prev_close": round(price / (1 + change_pct / 100), 2) if change_pct else None,
        "market_closed": market_closed,
        "url": url,
        "note": "TradingView 页面数据（可能为上一交易日收盘，非实时）",
    }


def get_quote(symbol: str, source: str = "auto") -> dict:
    symbol = symbol.upper().strip()
    in_session = is_us_regular_session()

    if source == "finnhub":
        return fetch_finnhub(symbol)
    if source == "tradingview":
        return fetch_tradingview(symbol)

    # auto: 盘中 Finnhub，收盘后 TradingView
    if in_session:
        try:
            return fetch_finnhub(symbol)
        except Exception as exc:
            result = fetch_tradingview(symbol)
            result["fallback_reason"] = str(exc)
            return result
    return fetch_tradingview(symbol)


def cmd_quote(symbol: str, source: str) -> None:
    in_session = is_us_regular_session()
    now_et = datetime.now(_ET)
    now_bj = datetime.now(_BJ)

    print(f"查询: {symbol.upper()}")
    print(f"美东时间: {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"北京时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"美股常规时段: {'开盘中' if in_session else '已收盘/非交易时段'} ({_bj_window_hint()})")
    print()

    q = get_quote(symbol, source)

    sign = "+" if (q.get("change_pct") or 0) >= 0 else ""
    print(f"数据源:   {q['source']}")
    print(f"现价:     ${q['price']:.2f}")
    if q.get("change_pct") is not None:
        print(f"涨跌幅:   {sign}{q['change_pct']:.2f}%")
    if q.get("change") is not None:
        ch_sign = "+" if q["change"] >= 0 else ""
        print(f"涨跌额:   {ch_sign}{q['change']:.2f}")
    if q.get("prev_close"):
        print(f"昨收:     ${q['prev_close']:.2f}")
    print(f"说明:     {q.get('note', '')}")
    if q.get("fallback_reason"):
        print(f"回退原因: {q['fallback_reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="美股行情（盘中 Finnhub / 收盘 TradingView）")
    parser.add_argument("symbol", help="股票代码，如 MU、AAPL")
    parser.add_argument(
        "--source",
        choices=["auto", "finnhub", "tradingview"],
        default="auto",
        help="数据源（默认 auto：盘中 Finnhub，收盘 TradingView）",
    )
    args = parser.parse_args()
    try:
        cmd_quote(args.symbol, args.source)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
