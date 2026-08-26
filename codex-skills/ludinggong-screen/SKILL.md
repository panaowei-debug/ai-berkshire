---
name: ludinggong-screen
description: "AI Berkshire skill: 鹿鼎公高股息价值选股. Source: skills/ludinggong-screen.md."
---

## Codex adapter note

This skill is generated from `skills/ludinggong-screen.md` so Claude Code and Codex users share one canonical workflow.

- Treat `$ARGUMENTS` as the user's request in the current Codex thread.
- When the source mentions Claude-only surfaces such as Task, Agent, WebSearch, Bash, Read, or Write, use the closest Codex capability available in this session: subagents when available, web search when needed, shell commands for local tools, and normal file edits for workspace files.
- Use shared project tools from `tools/` in this repository. Prefer running commands from the repository root with paths like `python3 tools/financial_rigor.py ...`; if the current thread starts outside the repo, locate the actual checkout path first instead of assuming a fixed home-directory path.
- Before starting research, run the `date` command to confirm today's date; treat it as the baseline for "latest" data and state the data cutoff date in the report header. Never assume the current date from training data.
- Preserve the research quality rules from `AGENTS.md`: cross-check financial data, use exact arithmetic tools for valuation/math, and clearly label uncertainty and source gaps.

# 鹿鼎公高股息价值选股

对 $ARGUMENTS 执行鹿鼎公体系量化选股（三低两高 + 国企偏好 + 高股息）。

## 执行前

1. 运行 `date` 确认数据截止日
2. 阅读 `docs/ludinggong-screen-methodology.md`

## 命令

```bash
# 生成当期选股池（约1分钟）
python3 tools/ludinggong_screener.py build --as-of $(date +%Y-%m-%d)

# 规则摘要
python3 tools/ludinggong_screener.py explain

# 导出CSV
python3 tools/ludinggong_screener.py export-csv
```

## 选股标准（十六字真言·可量化部分）

| 维度 | 鹿鼎公原意 | 量化规则 |
|------|-----------|----------|
| 价值选股 | 垄断+低估值+稳分红 | 三低两高硬过滤 |
| 估值定仓 | 低估重仓 | 股息率分层：≥7%超级低估，≥5%低估 |
| 趋势选时 | 宏观+微观共振 | **本工具不覆盖，需人工** |
| 波动降本 | 底仓做T | **本工具不覆盖，需人工** |

### 三低两高硬过滤

- 低PE：≤20
- 低PB：≤2.5（公用事业≤3.5）
- 低负债：≤60%（公用事业≤70%）
- 高股息：≥4%（公用事业≥3.5%）
- 高ROE：≥8%（优先年报数据）

### 排除项

- ST、亏损、市值<80亿、科技板块（信息技术/电子/计算机/传媒/通信）
- 偏好国企央企，谨慎民企

## 输出

```
data/ludinggong_pool/
├── latest.json
├── latest.csv
├── pool_by_code.csv
└── 鹿鼎公选股池首版-{YYYYMMDD}.md
```

## 报告要求

1. 列出核心5只 + 超级低估名单
2. 标注每只的股息率、PE、估值层、是否国企
3. 明确说明：**趋势选时和做T需人工判断，工具只解决「买什么」**
4. 对每只核心标的附反面论据（周期高点风险、分红可持续性等）

## 与龙头池的关系

- `csi300_leader_pool`：按市值/流动性选行业龙头（底背离交易universe）
- `ludinggong_pool`：按鹿鼎公价值标准选高股息防御标的（长期底仓候选）
- 两者交集 = 既有流动性又有价值安全边际的标的
