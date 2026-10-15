---
name: flex-date-saver
description: 弹性日期省钱规划。用户给出常住城市、目的地（最多2个）、时间窗口、玩几晚、可请假日期、下班时间与通勤、交通与酒店偏好后，调用飞猪 flyai-cli 的真实机票、火车、酒店报价，计算请假最少、总价最低或可玩时段最多的前3个出发方案，并给出总价拆分、可玩时段、风险标签和预订链接。用户提到「哪天出发最便宜」「拼假」「错峰」「请几天假划算」「弹性日期」「年假怎么安排出去玩」或粘贴【规划需求单 v1】时使用。不用于：帮用户选目的地、排景点行程、查实时余票、代下单、签证。
---

# 弹性日期省钱规划

本技能的脚本负责全部计算（请假天数、候选日期、查价过滤、总价、可玩时段、排序），智能体只负责理解需求、复述确认和解释结果。

## 环境要求

- 需要 `@fly-ai/flyai-cli@1.0.16`（1.0.5 等旧版没有 search-train）。使用前先执行 `flyai --help | grep -c search-train`，输出 0 就提示用户运行 `npm i -g @fly-ai/flyai-cli@1.0.16`；也可用环境变量 `FLYAI_BIN` 指向 1.0.16 的可执行文件
- 需要能访问外网（百炼运行环境把网络策略设为 unrestricted）
- 飞猪 Key 用会话环境变量 `FLYAI_API_KEY` 或 `flyai config set FLYAI_API_KEY ...` 配置；不要用密钥库（会 401）
- Python 3.8+，无第三方依赖

## 交互要求（不论在百炼还是本机调用）

- 先收集约束：用户粘贴了「【规划需求单 v1】」就直接用；否则只列出缺项并给默认值，一次问完，也可以给表单链接 https://thinsten.github.io/flex-date-saver/web/
- 查价前用一张表复述条件，用户确认后再运行 run_plan.py
- 输出：一句话结论 → 前 3 方案对比表（出发→返回｜请假｜可玩时段｜总价｜比最贵省｜风险）→ 每个方案的去回程、酒店、可玩时段、预订链接 → 各目的地最优（两个目的地时）→ 为什么有些日期没有方案 → notes

## 使用步骤

以下命令都在本技能目录下执行（先 `cd` 到 SKILL.md 所在目录）。

1. 把用户的需求单（JSON，或表单复制的整段文本）写入 `/tmp/fds_run/request.json`
2. 先校验：
   `python3 scripts/validate_request.py --request /tmp/fds_run/request.json --out /tmp/fds_run/request.normalized.json`
   - 输出 `ok=false` 时，把 `missing`（缺项）和 `conflicts`（冲突及 suggestions）一次性问用户，不要逐项追问
   - `warnings` 和 `applied` 要在复述确认时告诉用户
3. 用户确认后再查价（需要约 1 分钟）：
   `python3 scripts/run_plan.py --request /tmp/fds_run/request.json --out /tmp/fds_run/plan.json --workdir /tmp/fds_run`
4. 读取 `/tmp/fds_run/plan.json`，按系统提示词的输出模板展示
5. 用户只改目标（省钱/多玩/兼顾）时，不重新查价：
   `python3 scripts/score_rank.py --request /tmp/fds_run/request.normalized.json --quotes /tmp/fds_run/quotes.json --out /tmp/fds_run/plan.json --goal play`
6. 用户改了日期、出发地、酒店、交通等条件：改写 request.json 后重跑第 3 步，已查过的会命中缓存

## plan.json 关键字段

- `plans[]`：前 3 个方案。`price.total` 总价、`price` 拆分、`slot_count` 可玩时段数、`slots` 按日期列出时段、`leave_dates` 请假日期、`save_vs_max` 比最贵方案省多少、`flags` 风险标签、`out`/`ret` 去回程（含火车段与 `url`）、`hotel`（含 `url`、`pic`）、`reason_facts` 与相邻名次的价差/时段差/请假差
- `per_destination_best`：两个目的地时各自最优
- `unavailable_hints`：为什么有些日期没方案、怎么放宽
- `warnings` / `errors`：查询中的提示和错误；`replay=true` 表示用的是回放数据
- `notes`：必须展示的数据边界说明

## 规则

- 价格、时段、请假天数、航班号、车次、链接只能来自 plan.json，禁止估算或编造
- `errors` 含 `key_invalid` / `trial_mode` / `cli_missing` / `cli_too_old` 时，不展示任何价格，说明原因（版本过旧就给出升级命令）
- `errors` 含 `risk_control` 时，告诉用户飞猪接口短时调用过多，约 10 分钟后再试，已拿到的方案照常展示
- `plans` 为空时，依据 `unavailable_hints` 给出放宽建议让用户选
