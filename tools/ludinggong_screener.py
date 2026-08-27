#!/usr/bin/env python3
"""鹿鼎公高股息价值选股工具（三低两高）。

用法:
    python3 tools/ludinggong_screener.py build --as-of 2026-08-26
    python3 tools/ludinggong_screener.py explain
    python3 tools/ludinggong_screener.py export-csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ashare_data import (  # noqa: E402
    fetch_dividend_annual_batch,
    fetch_index_constituents,
    fetch_latest_financials_batch,
    fetch_org_basicinfo_batch,
    fetch_org_detail_batch,
    fetch_quotes_batch,
    fetch_valuation_em_batch,
)

POOL_DIR = ROOT / "data" / "ludinggong_pool"
CRITERIA_FILE = POOL_DIR / "criteria.json"
LATEST_JSON = POOL_DIR / "latest.json"
LATEST_CSV = POOL_DIR / "latest.csv"
BY_CODE_CSV = POOL_DIR / "pool_by_code.csv"
HISTORY_DIR = POOL_DIR / "history"


def load_criteria() -> dict:
    with open(CRITERIA_FILE, encoding="utf-8") as f:
        return json.load(f)


def safe_float(value, default=0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def is_st(name: str) -> bool:
    u = (name or "").upper()
    return "ST" in u or u.startswith("*")


def chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def pct_rank(values: dict[str, float], higher_better: bool) -> dict[str, float]:
    if not values:
        return {}
    items = sorted(values.items(), key=lambda x: x[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 100.0}
    out = {}
    for i, (code, _) in enumerate(items):
        score = i / (n - 1) * 100
        out[code] = score if higher_better else 100 - score
    return out


def build_universe(criteria: dict) -> list[dict]:
    seen = {}
    for idx in criteria["universe"]["indices"]:
        for row in fetch_index_constituents(idx):
            seen[row["code"]] = row
    return list(seen.values())


def is_soe(org: dict, criteria: dict) -> bool:
    text = f"{org.get('org_form','')} {org.get('real_controller','')} {org.get('control_holder','')}"
    return any(k in text for k in criteria["soe_keywords"])


def is_private_risk(org: dict, criteria: dict) -> bool:
    text = f"{org.get('org_form','')} {org.get('real_controller','')}"
    if any(k in text for k in criteria["private_keywords"]):
        if not is_soe(org, criteria):
            return True
    return False


def is_tech(em2016: str, criteria: dict) -> bool:
    level1 = (em2016 or "").split("-")[0]
    return level1 in criteria["excluded_em2016_level1"]


def is_utility(em2016: str, board: str) -> bool:
    text = f"{em2016} {board}"
    return any(k in text for k in ("公用事业", "电力", "水电", "火电"))


def valuation_tier(dy: float, criteria: dict) -> str:
    tiers = criteria["valuation_tiers"]
    if dy >= tiers["super_undervalued_dividend_yield"]:
        return "超级低估"
    if dy >= tiers["undervalued_dividend_yield"]:
        return "低估"
    if dy >= tiers["fair_low_dividend_yield"]:
        return "合理偏低"
    return "不达标"


def calc_pe_static(market_cap_yi: float, net_profit_yuan: float) -> float:
    """PE(静) = 总市值 / 年报归母净利润，与雪球/TradingView 一致。"""
    if market_cap_yi <= 0 or net_profit_yuan <= 0:
        return 0.0
    return market_cap_yi / (net_profit_yuan / 1e8)


def fetch_org_and_div_parallel(secucodes: list, as_of: str) -> tuple[dict, dict]:
    org = {}
    div = {}

    def _org(sc):
        return fetch_org_detail_batch([sc])

    def _div(sc):
        return fetch_dividend_annual_batch([sc], as_of)

    with ThreadPoolExecutor(max_workers=10) as ex:
        f_org = {ex.submit(_org, sc): sc for sc in secucodes}
        f_div = {ex.submit(_div, sc): sc for sc in secucodes}
        for fut in as_completed(f_org):
            try:
                org.update(fut.result())
            except Exception:
                pass
        for fut in as_completed(f_div):
            sc = f_div[fut]
            code = sc.split(".")[0]
            try:
                div.update(fut.result())
            except Exception:
                div[code] = {"div_annual": 0.0, "div_year": ""}
    return org, div


def cmd_build(as_of: str):
    criteria = load_criteria()
    hf = criteria["hard_filters"]
    weights = criteria["weights"]
    as_of_dt = datetime.strptime(as_of, "%Y-%m-%d")

    print(f"鹿鼎公选股 build @ {as_of}")
    constituents = build_universe(criteria)
    print(f"  股票池: {len(constituents)} 只（去重后）")

    codes = [c["code"] for c in constituents]
    secu_map = {c["code"]: c.get("secucode") or f"{c['code']}.SH" for c in constituents}

    quotes = {}
    for batch in chunk(codes, 60):
        quotes.update(fetch_quotes_batch(batch))

    financials = {}
    secucodes = list(secu_map.values())
    for batch in chunk(secucodes, 40):
        financials.update(fetch_latest_financials_batch(batch))
        time.sleep(0.05)

    industries = {}
    for batch in chunk(secucodes, 40):
        industries.update(fetch_org_basicinfo_batch(batch))
        time.sleep(0.05)

    valuations = {}
    for batch in chunk(secucodes, 40):
        valuations.update(fetch_valuation_em_batch(batch))
        time.sleep(0.05)

    pre = []
    for item in constituents:
        code = item["code"]
        q = quotes.get(code, {})
        fin = financials.get(code, {})
        ind = industries.get(code, {})
        val = valuations.get(code, {})
        name = q.get("name") or item.get("name", "")
        price = safe_float(q.get("price"))
        mcap = safe_float(q.get("market_cap"))
        net_profit = fin.get("net_profit", 0)
        pe_static = calc_pe_static(mcap, net_profit)
        pe_ttm = safe_float(val.get("pe_ttm")) or safe_float(q.get("pe"))
        pe = pe_static if pe_static > 0 else pe_ttm
        pb = safe_float(val.get("pb_mrq")) or safe_float(q.get("pb"))

        em2016 = ind.get("em2016") or ""
        utility = is_utility(em2016, "")
        pb_limit = hf.get("max_pb_utilities", hf["max_pb"]) if utility else hf["max_pb"]

        if not q or is_st(name):
            continue
        if price < hf["min_price"] or mcap < hf["min_market_cap_yi"]:
            continue
        if pe <= 0 or pe > hf["max_pe"] or pb <= 0 or pb > pb_limit:
            continue
        if fin.get("revenue", 0) < hf["min_revenue"]:
            continue
        if hf["require_profit"] and fin.get("net_profit", 0) <= 0:
            continue
        if fin.get("roe", 0) < hf["min_roe"]:
            continue

        if hf["exclude_tech"] and is_tech(em2016, criteria):
            continue

        debt_limit = hf["max_debt_ratio_utilities"] if utility else hf["max_debt_ratio"]
        if fin.get("debt_ratio", 100) > debt_limit:
            continue

        pre.append({
            "code": code,
            "name": name,
            "secucode": secu_map[code],
            "price": price,
            "pe": round(pe, 2),
            "pe_static": round(pe_static, 2) if pe_static > 0 else None,
            "pe_ttm": round(pe_ttm, 2) if pe_ttm > 0 else None,
            "pb": round(pb, 2),
            "market_cap_yi": mcap,
            "roe": fin.get("roe", 0),
            "debt_ratio": fin.get("debt_ratio", 0),
            "revenue": fin.get("revenue", 0),
            "net_profit": fin.get("net_profit", 0),
            "report_date": fin.get("report_date", ""),
            "em2016": em2016,
            "industry_level1": ind.get("industry_level1") or em2016.split("-")[0],
        })

    print(f"  行情+财报预筛: {len(pre)} 只，拉取分红与企业性质...", flush=True)
    secu_list = [r["secucode"] for r in pre]
    org_map, div_map = fetch_org_and_div_parallel(secu_list, as_of)

    rows = []
    reject_reasons = {"低股息": 0, "民企谨慎": 0}
    for row in pre:
        code = row["code"]
        org = org_map.get(code, {})
        div_info = div_map.get(code, {})
        div_annual = div_info.get("div_annual", 0.0)
        div_year = div_info.get("div_year", "")
        dy = div_annual / row["price"] * 100 if row["price"] > 0 else 0.0
        utility = is_utility(org.get("em2016") or row["em2016"], org.get("board_level1") or "")
        dy_min = hf.get("min_dividend_yield_utilities", hf["min_dividend_yield"]) if utility else hf["min_dividend_yield"]

        if dy < dy_min:
            reject_reasons["低股息"] += 1
            continue

        soe = is_soe(org, criteria)
        private_risk = is_private_risk(org, criteria)
        if hf["prefer_soe"] and private_risk:
            reject_reasons["民企谨慎"] += 1
            continue

        em2016 = org.get("em2016") or row["em2016"]
        board = org.get("board_level1") or row["industry_level1"]
        concepts = org.get("concepts") or ""
        tier = valuation_tier(dy, criteria)

        rows.append({
            **row,
            "em2016": em2016,
            "industry_level1": board or row["industry_level1"],
            "org_form": org.get("org_form", ""),
            "real_controller": org.get("real_controller", ""),
            "div_annual": round(div_annual, 4),
            "div_year": div_year,
            "dividend_yield": round(dy, 2),
            "valuation_tier": tier,
            "is_soe": soe,
            "concepts": concepts,
        })

    print(f"  硬过滤后: {len(rows)} 只（低股息剔除 {reject_reasons['低股息']}，民企谨慎剔除 {reject_reasons['民企谨慎']}）")

    if not rows:
        print("  ⚠️ 无标的通过，请检查阈值或数据源")
        return

    pe_rank = pct_rank({r["code"]: r["pe"] for r in rows}, False)
    pb_rank = pct_rank({r["code"]: r["pb"] for r in rows}, False)
    debt_rank = pct_rank({r["code"]: r["debt_ratio"] for r in rows}, False)
    roe_rank = pct_rank({r["code"]: r["roe"] for r in rows}, True)
    dy_rank = pct_rank({r["code"]: r["dividend_yield"] for r in rows}, True)

    preferred = set(criteria["preferred_sectors"])
    for r in rows:
        base = (
            dy_rank[r["code"]] * weights["dividend_yield"]
            + roe_rank[r["code"]] * weights["roe"]
            + pe_rank[r["code"]] * weights["pe"]
            + pb_rank[r["code"]] * weights["pb"]
            + debt_rank[r["code"]] * weights["debt_ratio"]
        )
        bonus = 0.0
        if any(p in (r["industry_level1"] or "") for p in preferred) or any(
            p in (r["em2016"] or "") for p in preferred
        ):
            bonus += 5
        if r["is_soe"]:
            bonus += 5
        if "红利股" in (r.get("concepts") or ""):
            bonus += 3
        r["score"] = round(min(base + bonus, 100), 2)
        r["rank"] = 0

    rows.sort(key=lambda x: (-x["score"], -x["dividend_yield"], x["pe"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    core = rows[: criteria["position_rules"]["core_holdings"]]

    result = {
        "as_of": as_of,
        "strategy": "鹿鼎公高股息价值选股",
        "count": len(rows),
        "core_holdings": [{"code": r["code"], "name": r["name"], "score": r["score"],
                           "dividend_yield": r["dividend_yield"], "valuation_tier": r["valuation_tier"]}
                          for r in core],
        "meta": {
            "universe_size": len(constituents),
            "prefilter_count": len(pre),
            "reject_reasons": reject_reasons,
            "criteria_version": criteria.get("version", "1.0"),
        },
        "stocks": rows,
    }

    POOL_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    fields = [
        "rank", "code", "name", "score", "valuation_tier", "dividend_yield", "roe",
        "pe", "pb", "debt_ratio", "market_cap_yi", "industry_level1", "is_soe", "org_form",
    ]
    with open(LATEST_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    with open(BY_CODE_CSV, "w", encoding="utf-8-sig", newline="") as f:
        sorted_rows = sorted(rows, key=lambda x: x["code"])
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted_rows)

    hist = HISTORY_DIR / f"{as_of}.json"
    with open(hist, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成: {len(rows)} 只 -> {LATEST_JSON}")
    print(f"  核心5只: {', '.join(r['name'] for r in core)}")
    print("\n  Top10:")
    for r in rows[:10]:
        print(f"    {r['rank']:>2}. {r['code']} {r['name']:<8} 分{r['score']:.1f} "
              f"股息{r['dividend_yield']:.1f}% {r['valuation_tier']} PE{r['pe']:.1f}")


def cmd_explain():
    c = load_criteria()
    print("鹿鼎公高股息价值选股 — 规则摘要")
    print("详见 docs/ludinggong-screen-methodology.md\n")
    hf = c["hard_filters"]
    print("硬过滤:")
    print(f"  PE≤{hf['max_pe']} PB≤{hf['max_pb']} 负债率≤{hf['max_debt_ratio']}%")
    print(f"  股息率≥{hf['min_dividend_yield']}% ROE≥{hf['min_roe']}% 市值≥{hf['min_market_cap_yi']}亿")
    print(f"  排除科技: {hf['exclude_tech']} 偏好国企: {hf['prefer_soe']}")
    print(f"\n权重: {c['weights']}")


def cmd_export_csv():
    if not LATEST_JSON.exists():
        print("请先运行 build")
        return
    with open(LATEST_JSON, encoding="utf-8") as f:
        data = json.load(f)
    rows = sorted(data.get("stocks", []), key=lambda x: x["code"])
    fields = ["code", "name", "dividend_yield", "score", "valuation_tier", "pe", "pb", "roe"]
    out = POOL_DIR / f"鹿鼎公选股-{data.get('as_of','')}.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"导出 {len(rows)} 只 -> {out}")


def main():
    parser = argparse.ArgumentParser(description="鹿鼎公高股息价值选股")
    sub = parser.add_subparsers(dest="command")
    p_build = sub.add_parser("build", help="生成当期选股池")
    p_build.add_argument("--as-of", default=datetime.now().strftime("%Y-%m-%d"))
    sub.add_parser("explain", help="规则摘要")
    sub.add_parser("export-csv", help="导出CSV")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    if args.command == "build":
        cmd_build(args.as_of)
    elif args.command == "explain":
        cmd_explain()
    elif args.command == "export-csv":
        cmd_export_csv()


if __name__ == "__main__":
    main()
