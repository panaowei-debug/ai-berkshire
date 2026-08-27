#!/usr/bin/env python3
"""A股数据工具 — 腾讯行情 + 东方财富搜索/财务，零外部依赖（仅 stdlib）。

为 Claude Code Skills 提供 A 股实时行情、财务数据等数据。
设计原则：独立模块，不影响现有工具；使用 curl 直连绕过系统代理。

用法（由 Skills 自动调用）：
    python3.11 tools/ashare_data.py quote 600519                    # 实时行情
    python3.11 tools/ashare_data.py financials 600519               # 核心财务数据（近5年）
    python3.11 tools/ashare_data.py valuation 600519                # 估值指标
    python3.11 tools/ashare_data.py search 茅台                      # 搜索股票代码

需要 Python >= 3.8，零外部依赖。
"""

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_EVEN

_TIMEOUT = 30


def _curl(url, headers=None):
    """用 curl --noproxy 直连，绕过系统代理。"""
    cmd = ["/usr/bin/curl", "-s", "--noproxy", "*", url]
    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    else:
        cmd.extend(["-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"])
    result = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("gbk")


def _curl_json(url, params=None):
    """curl 获取 JSON。"""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    return json.loads(_curl(url))


# ---------------------------------------------------------------------------
# 腾讯行情 API（稳定可靠，无需鉴权）
# ---------------------------------------------------------------------------

def _qq_code(code: str) -> str:
    """将股票代码转为腾讯行情格式。"""
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "2", "1")):
        return f"sz{code}"
    elif code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sh{code}"


def _parse_qq_quote(raw: str) -> dict:
    """解析腾讯行情数据。格式：v_shXXXXXX="字段1~字段2~..."; """
    start = raw.find('"')
    end = raw.rfind('"')
    if start < 0 or end <= start:
        return {}
    fields = raw[start + 1:end].split("~")
    if len(fields) < 50:
        return {}
    return {
        "name": fields[1],
        "code": fields[2],
        "price": fields[3],
        "prev_close": fields[4],
        "open": fields[5],
        "volume": fields[6],         # 手
        "buy_vol": fields[7],
        "sell_vol": fields[8],
        "high": fields[33] if len(fields) > 33 else fields[3],
        "low": fields[34] if len(fields) > 34 else fields[3],
        "change_pct": fields[32],
        "change_amt": fields[31],
        "turnover_amt": fields[37] if len(fields) > 37 else "-",
        "turnover_rate": fields[38] if len(fields) > 38 else "-",
        "pe": fields[39] if len(fields) > 39 else "-",
        "market_cap": fields[45] if len(fields) > 45 else "-",    # 总市值（亿）
        "float_cap": fields[44] if len(fields) > 44 else "-",     # 流通市值（亿）
        "pb": fields[46] if len(fields) > 46 else "-",
        "high_52w": fields[47] if len(fields) > 47 else "-",
        "low_52w": fields[48] if len(fields) > 48 else "-",
        "total_shares": fields[38] if len(fields) > 38 else "-",  # will recalculate
    }


def _fmt_yi(value) -> str:
    if value is None or value == "-" or value == "":
        return "-"
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"


def _fmt_pct(value) -> str:
    if value is None or value == "-" or value == "":
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (ValueError, TypeError):
        return str(value)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_quote(code: str):
    """实时行情快照。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    if not d:
        print(f"❌ 未找到股票 {code}")
        return

    print("=" * 60)
    print(f"实时行情: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  当前价:     {d['price']}")
    print(f"  涨跌幅:     {d['change_pct']}%")
    print(f"  涨跌额:     {d['change_amt']}")
    print(f"  今开:       {d['open']}")
    print(f"  最高:       {d['high']}")
    print(f"  最低:       {d['low']}")
    print(f"  昨收:       {d['prev_close']}")
    print(f"  成交量:     {d['volume']} 手")
    print(f"  成交额:     {d['turnover_amt']}万")
    print(f"  总市值:     {d['market_cap']}亿")
    print(f"  流通市值:   {d['float_cap']}亿")
    print(f"  PE(动):     {d['pe']}")
    print(f"  PB:         {d['pb']}")
    print(f"  换手率:     {d['turnover_rate']}%")
    print(f"  52周最高:   {d['high_52w']}")
    print(f"  52周最低:   {d['low_52w']}")


def cmd_valuation(code: str):
    """估值指标汇总。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    if not d:
        print(f"❌ 未找到股票 {code}")
        return

    price = d["price"]
    market_cap_yi = d["market_cap"]

    print("=" * 60)
    print(f"估值指标: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  当前价:     {price}")
    print(f"  总市值:     {market_cap_yi}亿")
    print(f"  流通市值:   {d['float_cap']}亿")
    print(f"  PE(动):     {d['pe']}")
    print(f"  PB:         {d['pb']}")
    print(f"  52周最高:   {d['high_52w']}")
    print(f"  52周最低:   {d['low_52w']}")

    # 市值验算
    try:
        p = Decimal(price)
        cap = Decimal(market_cap_yi) * Decimal("1e8")
        shares = cap / p
        print(f"\n  推算总股本: {_fmt_yi(float(shares))}股")
        calc_cap = p * shares
        reported_cap = Decimal(market_cap_yi) * Decimal("1e8")
        diff = abs(calc_cap - reported_cap) / reported_cap * 100
        print(f"  市值验算:   ✅ 一致（推算法，偏差 {float(diff):.1f}%）")
    except Exception:
        pass


def cmd_financials(code: str):
    """近5年核心财务数据。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    name = d.get("name", code) if d else code

    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    market = "SH" if code_clean.startswith(("6", "9", "5")) else "SZ"

    # 东方财富 datacenter API（年报数据）
    fin_url = "https://datacenter.eastmoney.com/securities/api/data/get"
    params = {
        "type": "RPT_F10_FINANCE_MAINFINADATA",
        "sty": "ALL",
        "filter": f'(SECUCODE="{code_clean}.{market}")(REPORT_TYPE="年报")',
        "p": "1",
        "ps": "5",
        "sr": "-1",
        "st": "REPORT_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    reports = []
    try:
        data = _curl_json(fin_url, params)
        reports = data.get("result", {}).get("data", [])
    except Exception:
        pass

    # 如果年报筛选无结果，去掉年报限制
    if not reports:
        params["filter"] = f'(SECUCODE="{code_clean}.{market}")'
        try:
            data = _curl_json(fin_url, params)
            reports = data.get("result", {}).get("data", [])
        except Exception:
            pass

    print("=" * 60)
    print(f"核心财务数据: {name} ({code_clean})")
    print("=" * 60)

    if not reports:
        print("  ⚠️ 未能获取财务数据，建议通过 WebSearch 补充")
        return

    for r in reports[:5]:
        date = r.get("REPORT_DATE", "")[:10]
        report_name = r.get("REPORT_DATE_NAME", "")
        revenue = r.get("TOTALOPERATEREVE")
        net_profit = r.get("PARENTNETPROFIT")
        eps = r.get("EPSJB")
        bps = r.get("BPS")
        roe = r.get("ROEJQ")
        rev_growth = r.get("TOTALOPERATEREVETZ")
        profit_growth = r.get("PARENTNETPROFITTZ")

        print(f"\n  --- {date} {report_name} ---")
        if revenue is not None:
            print(f"  营收:           {_fmt_yi(revenue)}")
        if rev_growth is not None:
            print(f"  营收增速:       {_fmt_pct(rev_growth)}")
        if net_profit is not None:
            print(f"  归母净利润:     {_fmt_yi(net_profit)}")
        if profit_growth is not None:
            print(f"  净利润增速:     {_fmt_pct(profit_growth)}")
        if eps is not None:
            print(f"  基本每股收益:   {eps}")
        if bps is not None:
            print(f"  每股净资产:     {bps:.2f}")
        if roe is not None:
            print(f"  ROE(加权):      {_fmt_pct(roe)}")


def _market_suffix(code: str) -> str:
    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    market = "SH" if code_clean.startswith(("6", "9", "5")) else "SZ"
    return code_clean, market


def _em_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Referer": "https://quote.eastmoney.com/",
    }


def fetch_index_constituents(index_code: str = "000300") -> list:
    """获取指数成分股列表。返回 [{code, name, secucode}, ...]"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_INDEX_CONSTITUENT",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR",
        "filter": f'(INDEX_CODE="{index_code}")',
        "pageNumber": "1",
        "pageSize": "500",
        "sortTypes": "",
        "sortColumns": "",
    }
    from urllib.parse import urlencode
    raw = _curl(f"{url}?{urlencode(params)}", headers=_em_headers())
    data = json.loads(raw)
    rows = data.get("result", {}).get("data", []) or []
    out = []
    for row in rows:
        code = row.get("SECURITY_CODE", "")
        if not code:
            continue
        out.append({
            "code": code,
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "secucode": row.get("SECUCODE", ""),
        })
    return out


def fetch_quotes_batch(codes: list) -> dict:
    """批量获取腾讯行情。返回 {code: quote_dict}"""
    if not codes:
        return {}
    qq_codes = ",".join(_qq_code(c) for c in codes)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_codes}")
    out = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line or "~" not in line:
            continue
        parsed = _parse_qq_quote(line)
        if parsed.get("code"):
            out[parsed["code"]] = parsed
    return out


def fetch_kline_avg_turnover(code: str, days: int = 60) -> float:
    """近 N 日日均成交额（元）。基于腾讯前复权日 K 估算。"""
    qq = _qq_code(code)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={qq},day,,,{days},qfq"
    )
    raw = _curl(url)
    data = json.loads(raw)
    stock = data.get("data", {}).get(qq, {})
    rows = stock.get("qfqday") or stock.get("day") or []
    if not rows:
        return 0.0
    amounts = []
    for row in rows[-days:]:
        if len(row) < 6:
            continue
        try:
            close = float(row[1])
            volume_hands = float(row[5])
        except (TypeError, ValueError):
            continue
        amounts.append(volume_hands * 100 * close)
    return sum(amounts) / len(amounts) if amounts else 0.0


def fetch_org_basicinfo_batch(secucodes: list) -> dict:
    """批量获取 EM2016 行业。返回 {code: {em2016, industry_level1, name}}"""
    if not secucodes:
        return {}
    quoted = ",".join(f'"{s}"' for s in secucodes)
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_F10_ORG_BASICINFO",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,EM2016",
        "filter": f"(SECUCODE in ({quoted}))",
        "pageNumber": "1",
        "pageSize": str(max(10, len(secucodes))),
    }
    from urllib.parse import urlencode
    raw = _curl(f"{url}?{urlencode(params)}", headers=_em_headers())
    data = json.loads(raw)
    rows = data.get("result", {}).get("data", []) or []
    out = {}
    for row in rows:
        code = row.get("SECURITY_CODE")
        if not code:
            continue
        em2016 = row.get("EM2016") or ""
        out[code] = {
            "em2016": em2016,
            "industry_level1": em2016.split("-")[0] if em2016 else "",
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "list_date": "",
        }
    return out


def fetch_stock_industry(code: str) -> dict:
    """获取东财行业分类 EM2016（单股）。"""
    code_clean, market = _market_suffix(code)
    secucode = f"{code_clean}.{market}"
    info = fetch_org_basicinfo_batch([secucode]).get(code_clean, {})
    return info


def fetch_latest_financials_batch(secucodes: list) -> dict:
    """批量获取最新财务：优先年报 ROE/负债率，否则用最近一期。返回 {code: {...}}"""
    if not secucodes:
        return {}
    quoted = ",".join(f'"{s}"' for s in secucodes)
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": "SECUCODE,SECURITY_CODE,TOTALOPERATEREVE,PARENTNETPROFIT,ROEJQ,ZCFZL,REPORT_DATE",
        "filter": f"(SECUCODE in ({quoted}))",
        "pageNumber": "1",
        "pageSize": str(max(100, len(secucodes) * 5)),
        "sortTypes": "-1",
        "sortColumns": "REPORT_DATE",
    }
    from urllib.parse import urlencode
    raw = _curl(f"{url}?{urlencode(params)}", headers=_em_headers())
    data = json.loads(raw)
    rows = data.get("result", {}).get("data", []) or []
    grouped: dict[str, list] = {}
    for row in rows:
        code = row.get("SECURITY_CODE")
        if not code:
            continue
        grouped.setdefault(code, []).append(row)

    out = {}
    for code, code_rows in grouped.items():
        annual = [r for r in code_rows if "-12-31" in str(r.get("REPORT_DATE", ""))]
        pick = annual[0] if annual else code_rows[0]
        out[code] = {
            "revenue": float(pick.get("TOTALOPERATEREVE") or 0),
            "net_profit": float(pick.get("PARENTNETPROFIT") or 0),
            "roe": float(pick.get("ROEJQ") or 0),
            "debt_ratio": float(pick.get("ZCFZL") or 0),
            "report_date": (pick.get("REPORT_DATE") or "")[:10],
            "roe_is_annual": bool(annual),
        }
    return out


def fetch_org_detail_batch(secucodes: list) -> dict:
    """批量获取企业性质、实控人、概念标签。返回 {code: {...}}"""
    if not secucodes:
        return {}
    out = {}
    from urllib.parse import urlencode
    for i in range(0, len(secucodes), 1):
        secucode = secucodes[i]
        code = secucode.split(".")[0]
        params = {
            "type": "RPT_F10_ORG_BASICINFO",
            "sty": "ORG_FORM,REAL_CONTROLER,CONTROL_HOLDER,EM2016,BLGAINIAN,BOARD_NAME_1LEVEL,LISTING_DATE",
            "filter": f'(SECUCODE="{secucode}")',
            "p": "1",
            "ps": "1",
            "source": "HSF10",
            "client": "PC",
        }
        url = "https://datacenter.eastmoney.com/securities/api/data/get?" + urlencode(params)
        try:
            raw = _curl(url, headers=_em_headers())
            data = json.loads(raw)
            row = ((data.get("result") or {}).get("data") or [None])[0]
            if not row:
                continue
            out[code] = {
                "org_form": row.get("ORG_FORM") or "",
                "real_controller": row.get("REAL_CONTROLER") or "",
                "control_holder": row.get("CONTROL_HOLDER") or "",
                "em2016": row.get("EM2016") or "",
                "industry_level1": (row.get("EM2016") or "").split("-")[0],
                "board_level1": row.get("BOARD_NAME_1LEVEL") or "",
                "concepts": row.get("BLGAINIAN") or "",
                "listing_date": (row.get("LISTING_DATE") or "")[:10],
            }
        except Exception:
            continue
    return out


def _parse_cash_div_per_share(profile: str) -> float:
    """从分红方案文本解析每股现金分红，如 '10派10.30元' -> 1.03。"""
    import re
    if not profile:
        return 0.0
    m = re.search(r"10派\s*([\d.]+)\s*元", profile)
    if not m:
        return 0.0
    return float(m.group(1)) / 10.0


def fetch_dividend_annual_batch(secucodes: list, as_of: str | None = None) -> dict:
    """除权日滚动12个月现金分红合计（与雪球/TradingView 股息率口径一致）。

    在 as_of 前 365 天内，按除权日（EX_DIVIDEND_DATE）汇总已实施现金分红；
    无除权日时回退公告日（NOTICE_DATE）。
    返回 {code: {div_annual, div_year}}，其中 div_annual 为 TTM 每股分红。
    """
    if not secucodes:
        return {}
    from datetime import datetime, timedelta
    from urllib.parse import urlencode

    if as_of:
        as_of_dt = datetime.strptime(as_of[:10], "%Y-%m-%d")
    else:
        as_of_dt = datetime.now()
    window_start = as_of_dt - timedelta(days=365)

    out = {}
    for secucode in secucodes:
        code = secucode.split(".")[0]
        params = {
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "SECUCODE,SECURITY_CODE,NOTICE_DATE,EX_DIVIDEND_DATE,IMPL_PLAN_PROFILE",
            "filter": f'(SECUCODE="{secucode}")',
            "pageNumber": "1",
            "pageSize": "12",
            "sortTypes": "-1",
            "sortColumns": "EX_DIVIDEND_DATE",
        }
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urlencode(params)
        try:
            raw = _curl(url, headers=_em_headers())
            data = json.loads(raw)
            rows = (data.get("result") or {}).get("data") or []
            div_ttm = 0.0
            latest_ex = ""
            for row in rows:
                ex_raw = row.get("EX_DIVIDEND_DATE") or row.get("NOTICE_DATE") or ""
                ex = str(ex_raw)[:10]
                if not ex:
                    continue
                ex_dt = datetime.strptime(ex, "%Y-%m-%d")
                if not (window_start < ex_dt <= as_of_dt):
                    continue
                dps = _parse_cash_div_per_share(row.get("IMPL_PLAN_PROFILE") or "")
                if dps > 0:
                    div_ttm += dps
                    if not latest_ex or ex > latest_ex:
                        latest_ex = ex
            out[code] = {
                "div_annual": div_ttm,
                "div_year": latest_ex[:4] if latest_ex else "",
            }
        except Exception:
            out[code] = {"div_annual": 0.0, "div_year": ""}
    return out


def fetch_dividend_ttm_batch(secucodes: list, as_of: str | None = None) -> dict:
    """除权日滚动12个月现金分红合计。返回 {code: {div_ttm}}。"""
    annual = fetch_dividend_annual_batch(secucodes, as_of)
    return {code: {"div_ttm": v["div_annual"]} for code, v in annual.items()}


def fetch_valuation_em_batch(secucodes: list) -> dict:
    """批量获取东财估值：PE(TTM)、PB(MRQ)。返回 {code: {pe_ttm, pb_mrq, trade_date}}"""
    if not secucodes:
        return {}
    quoted = ",".join(f'"{s}"' for s in secucodes)
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "SECUCODE,SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ",
        "filter": f"(SECUCODE in ({quoted}))",
        "pageNumber": "1",
        "pageSize": str(max(50, len(secucodes) * 3)),
        "sortTypes": "-1",
        "sortColumns": "TRADE_DATE",
    }
    from urllib.parse import urlencode
    raw = _curl(f"{url}?{urlencode(params)}", headers=_em_headers())
    data = json.loads(raw)
    rows = data.get("result", {}).get("data", []) or []
    out = {}
    for row in rows:
        code = row.get("SECURITY_CODE")
        if not code or code in out:
            continue
        out[code] = {
            "pe_ttm": float(row.get("PE_TTM") or 0),
            "pb_mrq": float(row.get("PB_MRQ") or 0),
            "trade_date": (row.get("TRADE_DATE") or "")[:10],
        }
    return out


def cmd_index_constituents(index_code: str):
    rows = fetch_index_constituents(index_code)
    print("=" * 60)
    print(f"指数成分: {index_code}（共 {len(rows)} 只）")
    print("=" * 60)
    for row in rows[:20]:
        print(f"  {row['code']} {row['name']}")
    if len(rows) > 20:
        print(f"  ... 其余 {len(rows) - 20} 只")


def cmd_search(keyword: str):
    """搜索股票代码。"""
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    # Use env var or fall back to the public eastmoney search token
    token = os.environ.get("EASTMONEY_SEARCH_TOKEN") or "D43BF722C8E33BDC906FB84D85E326E8"
    params = {
        "input": keyword,
        "type": "14",
        "token": token,
        "count": "10",
    }
    data = _curl_json(url, params)
    results = data.get("QuotationCodeTable", {}).get("Data", [])

    if not results:
        print(f"❌ 未找到匹配 '{keyword}' 的股票")
        return

    print("=" * 60)
    print(f"搜索结果: '{keyword}'")
    print("=" * 60)
    for r in results:
        code = r.get("Code", "")
        name = r.get("Name", "")
        market = r.get("MktNum", "")
        mkt_label = {"1": "沪", "2": "深", "3": "北"}.get(str(market), "")
        print(f"  {code} {name} [{mkt_label}]")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="A股数据工具 — 腾讯行情 + 东方财富财务数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_quote = sub.add_parser("quote", help="实时行情")
    p_quote.add_argument("code", help="股票代码，如 600519")

    p_fin = sub.add_parser("financials", help="核心财务数据（近5年）")
    p_fin.add_argument("code", help="股票代码")

    p_val = sub.add_parser("valuation", help="估值指标")
    p_val.add_argument("code", help="股票代码")

    p_search = sub.add_parser("search", help="搜索股票代码")
    p_search.add_argument("keyword", help="公司名或关键词")

    p_index = sub.add_parser("index-constituents", help="指数成分股列表")
    p_index.add_argument("index_code", nargs="?", default="000300", help="指数代码，默认000300")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "quote": lambda: cmd_quote(args.code),
        "financials": lambda: cmd_financials(args.code),
        "valuation": lambda: cmd_valuation(args.code),
        "search": lambda: cmd_search(args.keyword),
        "index-constituents": lambda: cmd_index_constituents(args.index_code),
    }
    cmds[args.command]()


if __name__ == "__main__":
    main()
