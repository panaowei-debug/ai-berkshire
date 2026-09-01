#!/usr/bin/env python3
"""沪深300「20行业 × 5龙头」量化筛选工具。

用法:
    python3 tools/csi300_leader_pool.py build --as-of 2026-08-25
    python3 tools/csi300_leader_pool.py explain
    python3 tools/csi300_leader_pool.py compare-recall
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ashare_data import (  # noqa: E402
    fetch_index_constituents,
    fetch_kline_avg_turnover,
    fetch_latest_financials_batch,
    fetch_org_basicinfo_batch,
    fetch_quotes_batch,
)

POOL_DIR = ROOT / "data" / "csi300_leader_pool"
MAP_FILE = POOL_DIR / "industry_map.json"
LATEST_JSON = POOL_DIR / "latest.json"
LATEST_CSV = POOL_DIR / "latest.csv"
HISTORY_DIR = POOL_DIR / "history"
RECALL_README = ROOT / "筛选公司" / "A股召回池" / "README.md"

WEIGHTS = {
    "float_cap": 0.35,
    "turnover": 0.30,
    "revenue": 0.20,
    "roe": 0.15,
}


def load_industry_map() -> dict:
    with open(MAP_FILE, encoding="utf-8") as f:
        return json.load(f)


def map_strategy_industry(em2016: str, industry_map: dict) -> str:
    if not em2016:
        return industry_map["fallback"]
    parts = [p.strip() for p in em2016.split("-") if p.strip()]
    level1 = parts[0] if parts else ""
    level2 = parts[1] if len(parts) > 1 else ""

    l1_map = industry_map.get("em2016_level1_to_strategy", {})
    l2_overrides = industry_map.get("em2016_level2_overrides", {})

    if level1 in l2_overrides and level2:
        for key, target in l2_overrides[level1].items():
            if level2 == key or level2.startswith(key + "-"):
                return target

    if level1 == "信息技术" and "通信" in level2:
        return "通信"

    mapped = l1_map.get(level1)
    if mapped == "__SPLIT_BY_LEVEL2__":
        if level2.startswith("银行") or level2 == "银行":
            return "银行"
        return "非银金融"
    if mapped:
        return mapped

    sw_map = industry_map.get("sw_to_strategy", {})
    if level1 in sw_map:
        return sw_map[level1]
    return industry_map.get("fallback", "可选消费")


def pct_rank(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    items = sorted(values.items(), key=lambda x: x[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 100.0}
    out = {}
    for i, (code, _) in enumerate(items):
        out[code] = i / (n - 1) * 100
    return out


def is_st_name(name: str) -> bool:
    upper = name.upper()
    return "ST" in upper or upper.startswith("*")


def listing_days_ok(list_date: str, as_of: datetime) -> bool:
    if not list_date:
        return True
    try:
        listed = datetime.strptime(list_date, "%Y-%m-%d")
    except ValueError:
        return True
    return (as_of - listed).days >= 250


def safe_float(value, default=0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def enrich_universe(constituents: list, as_of: datetime, industry_map: dict) -> list[dict]:
    codes = [c["code"] for c in constituents]
    secucode_map = {c["code"]: c.get("secucode") or f"{c['code']}.SH" for c in constituents}

    print("  拉取行情...", flush=True)
    quotes = {}
    for batch in chunk(codes, 60):
        quotes.update(fetch_quotes_batch(batch))

    print("  拉取财报...", flush=True)
    financials = {}
    secucodes = list(secucode_map.values())
    for batch in chunk(secucodes, 40):
        financials.update(fetch_latest_financials_batch(batch))
        time.sleep(0.05)

    print("  拉取行业...", flush=True)
    industries = {}
    for batch in chunk(secucodes, 40):
        for code, info in fetch_org_basicinfo_batch(batch).items():
            industries[code] = info
        time.sleep(0.05)

    default_turnover = industry_map.get("default_turnover_threshold_yuan", 3e8)
    financial_turnover = industry_map.get("financial_turnover_threshold_yuan", 2e8)

    prefiltered = []
    for item in constituents:
        code = item["code"]
        quote = quotes.get(code, {})
        name = quote.get("name") or item.get("name", "")
        price = safe_float(quote.get("price"))
        float_cap_yi = safe_float(quote.get("float_cap"))
        float_cap = float_cap_yi * 1e8
        fin = financials.get(code, {})
        revenue = fin.get("revenue", 0.0)

        if not quote or is_st_name(name) or price < 3:
            continue
        if float_cap <= 0 or revenue <= 0:
            continue
        prefiltered.append({
            "code": code,
            "name": name,
            "secucode": secucode_map[code],
            "price": price,
            "float_cap": float_cap,
            "revenue": revenue,
            "roe": fin.get("roe", 0.0),
            "report_date": fin.get("report_date", ""),
            "industry_info": industries.get(code, {}),
            "quote": quote,
        })

    print(f"  预过滤后 {len(prefiltered)} 只，拉取60日成交额...", flush=True)
    turnover_map = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_kline_avg_turnover, row["code"], 60): row["code"]
            for row in prefiltered
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                turnover_map[code] = future.result()
            except Exception:
                q = next(r["quote"] for r in prefiltered if r["code"] == code)
                turnover_map[code] = safe_float(q.get("turnover_amt")) * 10000

    rows = []
    for row in prefiltered:
        code = row["code"]
        industry_info = row["industry_info"]
        strategy_industry = map_strategy_industry(
            industry_info.get("em2016", ""), industry_map
        )
        turnover_threshold = (
            financial_turnover if strategy_industry in ("银行", "非银金融") else default_turnover
        )
        avg_turnover = turnover_map.get(code, 0.0)
        if avg_turnover < turnover_threshold:
            continue

        rows.append({
            "code": code,
            "name": row["name"],
            "secucode": row["secucode"],
            "price": row["price"],
            "float_cap": row["float_cap"],
            "avg_turnover_60d": avg_turnover,
            "revenue": row["revenue"],
            "roe": row["roe"],
            "em2016": industry_info.get("em2016", ""),
            "industry_level1": industry_info.get("industry_level1", ""),
            "strategy_industry": strategy_industry,
            "report_date": row["report_date"],
        })
    return rows


def score_candidates(rows: list[dict]) -> list[dict]:
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_industry[row["strategy_industry"]].append(row)

    scored = []
    for industry, group in by_industry.items():
        float_rank = pct_rank({r["code"]: r["float_cap"] for r in group})
        turnover_rank = pct_rank({r["code"]: r["avg_turnover_60d"] for r in group})
        revenue_rank = pct_rank({r["code"]: r["revenue"] for r in group})
        roe_values = {r["code"]: max(r["roe"], 0.0) for r in group}
        roe_rank = pct_rank(roe_values)

        for row in group:
            code = row["code"]
            leader_score = (
                WEIGHTS["float_cap"] * float_rank[code]
                + WEIGHTS["turnover"] * turnover_rank[code]
                + WEIGHTS["revenue"] * revenue_rank[code]
                + WEIGHTS["roe"] * roe_rank[code]
            )
            item = dict(row)
            item["leader_score"] = round(leader_score, 2)
            item["industry_rank"] = 0
            scored.append(item)

    scored.sort(
        key=lambda r: (
            r["strategy_industry"],
            -r["leader_score"],
            -r["avg_turnover_60d"],
            -r["float_cap"],
        )
    )
    return scored


def select_leaders(scored: list[dict], industry_map: dict, previous: set[str] | None = None) -> list[dict]:
    previous = previous or set()
    by_industry: dict[str, list[dict]] = defaultdict(list)
    for row in scored:
        by_industry[row["strategy_industry"]].append(row)

    selected = []
    for industry, group in sorted(by_industry.items()):
        group = sorted(
            group,
            key=lambda r: (-r["leader_score"], -r["avg_turnover_60d"], -r["float_cap"]),
        )
        for idx, row in enumerate(group, start=1):
            row["industry_rank"] = idx

        cap = industry_map.get("bank_max", 3) if industry == "银行" else 5
        picks = []

        for row in group:
            if row["code"] in previous and row["industry_rank"] <= 7:
                picks.append(row)
        for row in group:
            if row in picks:
                continue
            if row["industry_rank"] <= cap:
                picks.append(row)

        if not picks:
            picks = group[:cap]
        else:
            picks = sorted(
                picks,
                key=lambda r: (-r["leader_score"], -r["avg_turnover_60d"], -r["float_cap"]),
            )[:cap]

        selected.extend(picks)
    return selected


def write_outputs(selected: list[dict], as_of: str, meta: dict):
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "as_of": as_of,
        "count": len(selected),
        "meta": meta,
        "stocks": selected,
    }
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(HISTORY_DIR / f"{as_of}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    fields = [
        "code", "name", "strategy_industry", "leader_score", "industry_rank",
        "price", "float_cap", "avg_turnover_60d", "revenue", "roe", "em2016",
    ]
    with open(LATEST_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in selected:
            writer.writerow({k: row.get(k, "") for k in fields})


def load_previous_codes() -> set[str]:
    if not LATEST_JSON.exists():
        return set()
    with open(LATEST_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return {s["code"] for s in data.get("stocks", [])}


def cmd_build(as_of: str, no_buffer: bool = False):
    as_of_dt = datetime.strptime(as_of, "%Y-%m-%d")
    industry_map = load_industry_map()
    print(f"拉取沪深300成分股... ({as_of})")
    constituents = fetch_index_constituents("000300")
    print(f"  成分股 {len(constituents)} 只")

    print("过滤并 enrich 候选池（约需 1-3 分钟）...")
    candidates = enrich_universe(constituents, as_of_dt, industry_map)
    print(f"  通过 U1-U6: {len(candidates)} 只")

    scored = score_candidates(candidates)
    previous = set() if no_buffer else load_previous_codes()
    selected = select_leaders(scored, industry_map, previous)
    print(f"  最终入选: {len(selected)} 只")

    industry_counts = defaultdict(int)
    for row in selected:
        industry_counts[row["strategy_industry"]] += 1

    meta = {
        "constituents": len(constituents),
        "candidates": len(candidates),
        "industry_counts": dict(sorted(industry_counts.items())),
        "weights": WEIGHTS,
    }
    write_outputs(selected, as_of, meta)

    print(f"\n已写入:\n  {LATEST_JSON}\n  {LATEST_CSV}")
    print("\n行业分布:")
    for industry, count in sorted(industry_counts.items()):
        print(f"  {industry}: {count}")


def _fmt_cap_yi(cap: float) -> str:
    if not cap:
        return "—"
    return f"{cap / 1e8:.0f}"


def render_pool_html(payload: dict) -> str:
    """生成适合长辈阅读的龙头池 HTML 页面。"""
    from collections import defaultdict
    import html as html_mod

    as_of = payload.get("as_of", "")
    stocks = payload.get("stocks", [])
    meta = payload.get("meta", {})
    industry_counts = meta.get("industry_counts", {})

    by_industry: dict[str, list] = defaultdict(list)
    for row in stocks:
        by_industry[row.get("strategy_industry", "其他")].append(row)
    for rows in by_industry.values():
        rows.sort(key=lambda x: x.get("industry_rank", 99))

    industry_order = sorted(by_industry.keys())

    industry_sections = []
    for industry in industry_order:
        rows = by_industry[industry]
        trs = []
        for r in rows:
            trs.append(
                "<tr>"
                f"<td class='code'>{html_mod.escape(r['code'])}</td>"
                f"<td class='name'>{html_mod.escape(r['name'])}</td>"
                f"<td class='rank'>第{int(r.get('industry_rank', 0))}名</td>"
                f"<td class='cap'>{_fmt_cap_yi(r.get('float_cap', 0))}亿</td>"
                f"<td class='price'>{r.get('price', 0):.2f}元</td>"
                "</tr>"
            )
        industry_sections.append(
            f"<section class='industry' data-industry='{html_mod.escape(industry)}'>"
            f"<h2>{html_mod.escape(industry)} <span class='badge'>{len(rows)}只</span></h2>"
            "<table><thead><tr>"
            "<th>代码</th><th>股票名称</th><th>行业排名</th><th>流通市值</th><th>股价</th>"
            "</tr></thead><tbody>"
            + "".join(trs)
            + "</tbody></table></section>"
        )

    industry_summary = "".join(
        f"<span class='tag'>{html_mod.escape(k)} {v}只</span>"
        for k, v in sorted(industry_counts.items())
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>沪深300龙头股票名单（{len(stocks)}只）</title>
  <style>
    :root {{
      --bg: #f7f5f0;
      --card: #ffffff;
      --text: #2c2c2c;
      --muted: #666;
      --accent: #1a6b4a;
      --accent-light: #e8f4ee;
      --border: #e0ddd6;
      --shadow: 0 2px 12px rgba(0,0,0,.06);
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 18px;
      line-height: 1.6;
      padding: 20px;
    }}
    .wrap {{ max-width: 960px; margin: 0 auto; }}
    header {{
      background: linear-gradient(135deg, #1a6b4a 0%, #2d8f66 100%);
      color: #fff;
      border-radius: 16px;
      padding: 28px 32px;
      margin-bottom: 24px;
      box-shadow: var(--shadow);
    }}
    header h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 8px; }}
    header .sub {{ font-size: 1rem; opacity: .92; }}
    header .meta {{ margin-top: 14px; font-size: .95rem; opacity: .85; }}
    .intro {{
      background: var(--card);
      border-radius: 12px;
      padding: 20px 24px;
      margin-bottom: 20px;
      border-left: 4px solid var(--accent);
      box-shadow: var(--shadow);
    }}
    .intro p {{ color: var(--muted); font-size: 1rem; }}
    .search-bar {{
      background: var(--card);
      border-radius: 12px;
      padding: 16px 20px;
      margin-bottom: 20px;
      box-shadow: var(--shadow);
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .search-bar label {{ font-weight: 600; white-space: nowrap; }}
    .search-bar input {{
      flex: 1;
      min-width: 200px;
      font-size: 1.1rem;
      padding: 10px 14px;
      border: 2px solid var(--border);
      border-radius: 8px;
      outline: none;
    }}
    .search-bar input:focus {{ border-color: var(--accent); }}
    .search-bar button {{
      font-size: 1rem;
      padding: 10px 18px;
      border: none;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      cursor: pointer;
    }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 24px; }}
    .tag {{
      background: var(--accent-light);
      color: var(--accent);
      padding: 4px 12px;
      border-radius: 20px;
      font-size: .85rem;
    }}
    .industry {{
      background: var(--card);
      border-radius: 12px;
      margin-bottom: 20px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }}
    .industry h2 {{
      font-size: 1.25rem;
      padding: 16px 20px;
      background: var(--accent-light);
      color: var(--accent);
      border-bottom: 1px solid var(--border);
    }}
    .badge {{
      font-size: .85rem;
      font-weight: normal;
      background: var(--accent);
      color: #fff;
      padding: 2px 10px;
      border-radius: 12px;
      margin-left: 8px;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid var(--border); }}
    th {{ background: #faf9f7; font-weight: 600; font-size: .9rem; color: var(--muted); }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fafdf9; }}
    .code {{ font-family: monospace; font-size: 1.05rem; color: var(--accent); font-weight: 600; }}
    .name {{ font-weight: 600; }}
    .rank {{ color: var(--muted); }}
    .cap, .price {{ color: var(--muted); font-size: .95rem; }}
    footer {{
      text-align: center;
      color: var(--muted);
      font-size: .85rem;
      padding: 24px 0 40px;
    }}
    .hidden {{ display: none !important; }}
    @media print {{
      body {{ background: #fff; font-size: 14px; }}
      .search-bar {{ display: none; }}
      header {{ background: #1a6b4a; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      .industry {{ break-inside: avoid; box-shadow: none; border: 1px solid #ddd; }}
    }}
    @media (max-width: 600px) {{
      body {{ font-size: 16px; padding: 12px; }}
      th, td {{ padding: 10px 12px; }}
      .cap, .price {{ display: none; }}
      th.cap-h, th.price-h, td.cap, td.price {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>沪深300龙头股票名单</h1>
      <p class="sub">从沪深300（A股最大的300家上市公司）中，按20个主流行业各选龙头企业，共 <strong>{len(stocks)}</strong> 只</p>
      <p class="meta">数据日期：{html_mod.escape(as_of)} · 仅供学习参考，不构成投资建议</p>
    </header>

    <div class="intro">
      <p>这份名单按<strong>行业</strong>分组，每个行业里排在前面的通常是市值更大、成交更活跃的龙头公司。可以用下面的搜索框，输入股票名称或代码快速查找。</p>
    </div>

    <div class="search-bar">
      <label for="q">🔍 搜索</label>
      <input id="q" type="search" placeholder="输入股票名称或代码，如：茅台、600519" autocomplete="off">
      <button type="button" onclick="window.print()">打印 / 存PDF</button>
    </div>

    <div class="tags">{industry_summary}</div>

    {"".join(industry_sections)}

    <footer>沪深300龙头池 · AI Berkshire 投研工具生成 · {html_mod.escape(as_of)}</footer>
  </div>
  <script>
    const q = document.getElementById('q');
    q.addEventListener('input', () => {{
      const term = q.value.trim().toLowerCase();
      document.querySelectorAll('.industry').forEach(sec => {{
        let any = false;
        sec.querySelectorAll('tbody tr').forEach(tr => {{
          const text = tr.textContent.toLowerCase();
          const show = !term || text.includes(term);
          tr.classList.toggle('hidden', !show);
          if (show) any = true;
        }});
        sec.classList.toggle('hidden', !any);
      }});
    }});
  </script>
</body>
</html>"""


def cmd_export_html():
    if not LATEST_JSON.exists():
        print("请先运行 build 生成 latest.json")
        return
    with open(LATEST_JSON, encoding="utf-8") as f:
        payload = json.load(f)
    as_of = payload.get("as_of", datetime.now().strftime("%Y-%m-%d"))
    html = render_pool_html(payload)
    out = POOL_DIR / f"沪深300龙头池{payload.get('count', 96)}只.html"
    out.write_text(html, encoding="utf-8")
    print(f"已导出 HTML -> {out}")


def cmd_explain():
    print("沪深300「20行业 × 5龙头」筛选规则摘要")
    print("详见 docs/csi300-leader-pool-methodology.md")
    print("\n评分权重:", WEIGHTS)
    print("输出目录:", POOL_DIR)


def parse_recall_pool_codes() -> set[str]:
    if not RECALL_README.exists():
        return set()
    text = RECALL_README.read_text(encoding="utf-8")
    return set(re.findall(r"\b(\d{6})\b", text))


def cmd_compare_recall():
    if not LATEST_JSON.exists():
        print("请先运行 build 生成 latest.json")
        return
    with open(LATEST_JSON, encoding="utf-8") as f:
        pool = json.load(f)
    pool_codes = {s["code"] for s in pool.get("stocks", [])}
    recall_codes = parse_recall_pool_codes()
    overlap = pool_codes & recall_codes
    rate = len(overlap) / len(recall_codes) * 100 if recall_codes else 0
    print(f"龙头池: {len(pool_codes)} 只")
    print(f"A股召回池: {len(recall_codes)} 只")
    print(f"重叠: {len(overlap)} 只 ({rate:.1f}%)")
    if overlap:
        print("重叠示例:", ", ".join(sorted(overlap)[:15]))


def main():
    parser = argparse.ArgumentParser(description="沪深300龙头池量化筛选")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="生成当期龙头池")
    p_build.add_argument("--as-of", default=datetime.now().strftime("%Y-%m-%d"))
    p_build.add_argument("--no-buffer", action="store_true", help="忽略上期缓冲区，全量重选")

    sub.add_parser("explain", help="打印规则摘要")
    sub.add_parser("compare-recall", help="与A股召回池对比重叠率")
    sub.add_parser("export-html", help="导出长辈可读 HTML 页面")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "build":
        cmd_build(args.as_of, args.no_buffer)
    elif args.command == "explain":
        cmd_explain()
    elif args.command == "compare-recall":
        cmd_compare_recall()
    elif args.command == "export-html":
        cmd_export_html()


if __name__ == "__main__":
    main()
