# AGENTS.md — flex-date-saver 维护手册

给 AI agent / 新协作者的上手文档。读完应能：本地改代码、本地验证、数据保留式部署、出问题回滚。

## 1. 项目是什么

「弹性日期省钱规划」：按请假、下班时间、通勤、红眼、中转等个人约束，用飞猪实时报价算出最省 / 最多玩的出发方案。三种形态共用同一套脚本：

| 形态 | 入口 | 说明 |
|---|---|---|
| 自部署网页（主用） | http://47.102.103.226:8088/ | 一句话 AI 填表 → 确认表单 → 开始规划 → 结果页；免登录，历史按浏览器隔离，可生成分享链接 |
| 百炼 Managed Agent | 智能体 `agent_01M4YWBRFG6Y0CRW8F8E5RN2YG` | 大赛作品形态，`tools/deploy_bailian.py` 部署 |
| 千问办公 Skill | `flex-date-saver.skill` | 本机对话调用，需全局 flyai-cli 1.0.16 |
| 静态表单（GitHub Pages） | https://thinsten.github.io/flex-date-saver/web/ | 只生成需求单文本，复制给智能体 |

- 仓库：github.com/thinsten/flex-date-saver（公开，gh CLI 已登录 thinsten）
- 服务器：与「和朋友凑个局」同一台阿里云轻量服务器（上海），互不影响：凑个局占 80/8090，本项目占 8088/8092

## 2. 目录地图

```
skill/flex-date-saver/          ← 核心：所有计算都在这里（三种形态共用）
  scripts/run_plan.py           ← 总入口：校验 → 候选 → 查价 → 排序 → plan.json
  scripts/validate_request.py   ← 需求单校验、缺项/冲突
  scripts/plan_candidates.py    ← 节假日、请假、候选日期
  scripts/fetch_quotes.py       ← 查价与过滤（航班不带时段参数！见坑表）
  scripts/flyai_client.py       ← flyai-cli 调用：限流、熔断、缓存、错误分类
  scripts/score_rank.py         ← 总价、时段、排序、风险标签
  config/defaults.json          ← 默认值与限流参数（rate_rules / max_calls / cooldown_s）
  config/holidays.json          ← 节假日（2027 为预估，官方公布后改这里）
server/app.py                   ← 网页后端（Python 标准库，无依赖）
web/index.html                  ← 表单页；后端注入 window.FDS_SERVER 后切换为网页模式
deploy/                         ← systemd、nginx、服务器安装脚本
tools/deploy_server.sh          ← 本地一键部署到服务器
tools/deploy_bailian.py         ← 部署到百炼
prompts/system_prompt.md        ← 百炼智能体系统提示词
tests/                          ← 单元测试 + 真实接口录制
docs/api-notes.md               ← 飞猪接口实测结论（改查价逻辑前先读）
data/                           ← 网页运行时数据。⚠️ 永不进 git、不打包部署
```

## 3. 网页后端设计

- **访问控制**：免登录（2026-10-15 用户要求去掉密码）。页面首次打开发一个 HttpOnly 匿名 cookie `fds_uid`（HMAC 签名，密钥 `FDS_SECRET`），任务 meta 记 uid：历史只列自己的，查看/切换目标要求是本人（分享链接除外）。防滥用靠每日总量 + 单 IP 上限（`FDS_IP_PLAN_LIMIT` 15、`FDS_IP_PARSE_LIMIT` 50）。
- **出发地与站点（高德）**：`/api/geo/ip`（IP→默认城市）、`/api/geo/tips`（地址联想）、`/api/geo/city`（城市→市中心坐标）、`/api/geo/stations`（出发点 100km 内机场+火车站，多边形搜索；去掉航站楼/通用/直升/建设中；只给机场和直线最近 8 个火车站算真实开车时间，其余按距离估算并折叠）。高德个人 Key 约 3 QPS：全局 0.35s 节流 + CUQPS 重试，结果内存缓存 1 天。前端用 geoSeq 只认最后一次请求的结果（IP/城市/地址会先后返回）。
- **开车直达邻近城市机场**：需求单 `stations_source=amap` 时，站点里机场所在城市 ≠ 出发城市的，自动作为 kind=drive 的航班出发城市；且不在站点列表里的机场一律不用（严格模式）。站名匹配用 `Geo._core`（「上海虹桥国际机场」「虹桥」→「虹桥」，「无锡硕放机场」「苏南硕放」→「硕放」）。
- **住宿只安排第一晚**：酒店按到达当晚查 1 晚；总价 = 交通 × 人数 + 第一晚 × 间数，其余晚数不计入（结果标注）。位置筛选 `hotel.location`：station（默认，离到达机场开车 ≤30 分钟：高德定位机场 → 查所在区县的酒店 → 直线粗筛 → 前 6 家算开车时间）、point（离指定地点 ≤N 公里）、none。没有满足的推荐最近的并打 too_far 标签。第一晚按每个到达机场分别挑（`hotels_by_arrival`）。没有 AMAP_KEY（百炼/本机 Skill）时自动退化为不限位置并提示。
- **日历**：`custom_workdays` 是用户把周末/假日改成上班（单休、调休），`HolidayCalendar.for_request(req)` 统一处理；`nights.options` 支持多选晚数。
- **一句话填表**：`/api/parse` 调百炼兼容接口（`FDS_LLM_MODEL`，默认 qwen3.7-plus；qwen-flash 实测会编造字段，别换回去），只输出用户说了的字段。「周五可以请假」这类由模型输出 weekdays，**后端展开成窗口内具体工作日**（不让模型数日历）。结果回填表单，用户确认后才规划——模型从不接触价格。
- **规划**：`/api/plan` 先 validate，不通过直接返回缺项/冲突；通过后入**单线程队列**，worker 逐个跑 run_plan.py（保证飞猪 QPS ≤2）。结果在 `data/jobs/<id>/`（request.json / plan.json / work/ / run.log / meta.json）。
- **切换目标**：`/api/job/<id>/goal` 用 work/quotes.json 重排，不查价。
- **分享**：`/share/<id>?k=<HMAC>` 免登录只读，换 FDS_SECRET 会让旧链接全部失效。
- **配额**：每天规划 `FDS_DAILY_PLAN_LIMIT`(80)、AI 填表 `FDS_DAILY_PARSE_LIMIT`(300)，进程内计数，重启归零。
- **重启**：排队/运行中的任务标记为失败，提示重新规划。

## 4. 本地验证（每次改动必做）

```bash
python3 -m unittest discover -s tests            # 38 个测试必须全过
# 本地起网页（用测试数据目录，密钥从本机文件读）：
set -a; . ~/.flex-date-saver.env; set +a
export DASHSCOPE_BASE_URL="https://$WS.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" FDS_PASSWORD=testpw \
  FDS_DATA_DIR=/tmp/fds-web-test FDS_PORT=8093 FLYAI_BIN=<flyai 1.0.16 路径> FLYAI_API_KEY=<从 ~/.flyai/config.json 读>
python3 server/app.py
# 安全检查（必须无输出）：
grep -rlE "sk-[A-Za-z0-9._-]{12,}" server web deploy skill tools/deploy_server.sh
```

浏览器走一遍：登录 → AI 填表 → 开始规划 → 结果页 → 切换目标 → 复制分享链接 → 无痕窗口打开分享链接。改了 web/index.html 还要确认 GitHub Pages 的静态模式（不注入 FDS_SERVER）仍能生成需求单。

## 5. 部署（数据保留式）

服务器布局：`/opt/flex-date-saver/{server,skill,web,deploy,node_modules,data,.releases}`，systemd 服务 `flex-date-saver`（127.0.0.1:8092，python3.11），nginx `conf.d/flex-date-saver.conf` 监听 8088。环境变量 `/etc/flex-date-saver.env`（root 600，含飞猪 Key、百炼 Key、高德 Key、FDS_SECRET；FDS_PASSWORD 已不再使用）。

```bash
tools/deploy_server.sh          # 只更新代码
tools/deploy_server.sh --env    # 同时更新 /etc/flex-date-saver.env（需先在本机生成 /tmp/fds.env，用完移到废纸篓）
```

remote-install.sh 的顺序：停服 → 备份 data/jobs 到 `.releases/<时间>/` → 旧代码挪进同一目录 → 解包 → 确保 flyai-cli 1.0.16（装在应用目录，不动全局） → systemd → nginx（`nginx -t` 通过才 reload，失败自动挪走新配置） → 健康检查，**同时检查凑个局 8090 仍为 200**。

上线后外网验证：`curl http://47.102.103.226:8088/healthz`。排障：`journalctl -u flex-date-saver -n 200`，单次规划日志在 `data/jobs/<id>/run.log`。

## 6. 安全红线

- data/、/etc/flex-date-saver.env、~/.flex-date-saver.env、/tmp/fds.env **永不进 git**；密码和 Key 不写进代码、文档、commit 信息。
- 前端不出现任何密钥；每次发布前跑第 4 节的 grep。
- 不改动凑个局的 nginx 配置和端口；部署脚本只动 `/opt/flex-date-saver`、自己的 conf 和 service。
- 删除一律挪到 `.releases/` 或废纸篓，不用 rm。

## 7. 回滚

- 代码：`/opt/flex-date-saver/.releases/<时间>/` 里有上一版 server/skill/web/deploy，挪回去后 `systemctl restart flex-date-saver`；或本地 git checkout 旧版本后重新部署。
- 数据：`.releases/<时间>/data-jobs.tar.gz` 解回 data/jobs。

## 8. 版本管理

- 每批改动一个 commit + push，中文概述。提交身份：
  `git -c user.name="thisten" -c user.email="thisten@users.noreply.github.com" commit -m "..."`
- 改了 skill/ 下的代码：网页重新部署；百炼要 `tools/deploy_bailian.py --only skills --new-version`；千问办公要重新打包 .skill。

## 9. 已知坑速查

| 坑 | 结论 |
|---|---|
| flyai-cli 版本 | 必须 1.0.16（1.0.5 无 search-train）；服务器装在 /opt/flex-date-saver/node_modules，用 FLYAI_BIN 指定 |
| 飞猪限流 | QPS=2；Key 已加白，rate_rules 只限 [[2,1.2]]。未加白的 Key 改回 [[2,1.2],[20,60],[40,600]]、gap_s [2.5,4.0] |
| 飞猪风控熔断 | 一旦 HTTP 451，所有进程冷却 20 分钟（状态文件 FDS_RATE_FILE）；本机、百炼、网页三处共用一个 Key，状态不共享，别同时大量查价 |
| 失败缓存 | 接口瞬时故障（data:null「搜索机票低价行业失败」，实测杭州-沈阳线按线×日间歇出现，9 分钟后同参数就有票）只缓存 `err_ttl_s`（60s），成功响应才按 `cache_ttl_s`（30min）；否则一次故障会把「当天无航班」锁 30 分钟 |
| 航班缓存 | 服务端 5 分钟内同线同日缓存且忽略时段参数，航班查询一律不带时段，本地过滤 |
| 往返 / 座席 / 区域 / 品牌 | 往返查询正式 Key 下为空（两段单程相加）；火车座席参数无效；酒店区域关键词无效；brandName 不可靠——详见 docs/api-notes.md |
| 服务器 Python | 系统自带 3.6 跑不了脚本（date.fromisoformat 等），服务用 /usr/bin/python3.11 |
| 百炼模型额度 | qwen-plus / qwen-max 免费额度已用完且开着「仅用免费额度」；可用 qwen3.7-plus、qwen3.8-max、qwen-flash。余额不足时接口 403/insufficient_quota |
| 外网访问 8088 | 阿里云轻量服务器防火墙已放行 TCP 8088（2026-10-15），系统内 firewalld 未启用 |
| 高德 QPS | 个人 Key 并发低，连续调用报 CUQPS_HAS_EXCEEDED_THE_LIMIT；server 与 amap_client 都有 0.35s 节流 + 重试，别改成大并发 |
| 高德 Key | AMAP_KEY 只在 /etc/flex-date-saver.env，服务端调用，前端不出现；建议在高德控制台给 Key 设 IP 白名单 47.102.103.226 |
| index.html 改动脚本 | 用 Python 精确替换时注意函数在文件里的先后顺序（曾因 index 顺序反了把代码插到了文件最前面） |
| scp | 加 -O 走传统协议，否则可能卡死 |
| 远程删除 | 部署脚本不用 rm，一律挪到 .releases/ |
