# AstrBot HLTV 插件需求与架构规范全集 (AGENT.md)

本文档系统汇总了用户对于本项目（`astrbot_plugin_hltv`）的所有业务需求、架构约束、技术实现规范与历史演进要求。后续任何参与本项目维护、升级或二次开发的 Agent 及开发者均需严格遵守本文档中阐明的各项规范。

---

## 1. 项目概述与定位

- **项目名称**：`astrbot_plugin_hltv`
- **运行环境**：基于 AstrBot 框架规范开发的独立插件（继承 `astrbot.api.star.Star` 类，遵循 `@filter.command_group` 指令规范）。
- **目录隔离规范（强制约束）**：
  - 本插件文件**必须严格且唯一**存在于插件子目录：
    `C:\Users\Rinyi\Documents\astrbot\data\plugins\astrbot_plugin_hltv\`
  - **严禁**在 AstrBot 根目录（`C:\Users\Rinyi\Documents\astrbot\`）放置任何插件源码或副本文件，根目录只应保留 AstrBot 本体架构目录（`.ruff_cache`, `data`, `upstream`）。

---

## 2. 接口服务与网络直连架构

- **API 接口地址**：`https://hltv.rinyin.top`
- **API 在线文档**：`https://hltv.rinyin.top/docs`
- **上游 API 契约**（插件依赖的行为）：
  - 比赛详情 `status` 依据页面 countdown（`Match over`/`LIVE`/倒计时）判定，不再因存在地图占位而误判为完赛。
  - `TBA` 地图占位不返回；未开赛比赛不返回选手数据；进行中比赛的大比分由已打完地图推算。
  - `format` 只返回 `boN`；弃权赛用 `forfeit: true` 表示。赛果列表带 `stars`（星数）。
  - `/api/v1/results?event=<ID>` 支持按赛事过滤，返回的比赛带 `event.id`。
- **网络防污染与直连机制**：
  - Windows 环境下常存在 TUN/Clash 等代理软件接管流量（产生 Fake-IP，导致 TLS 握手异常或 DNS 污染）。
  - 插件内置 `HostResolver`；用户在配置 `server_ip` 后，将 API 域名固定解析到该 IP。默认留空，使用系统 DNS。请求超时 15s 并有容错处理。

---

## 3. 核心功能与自动化调度需求

### 3.1 赛前 10 分钟自动预警提醒
- 自动持续轮询已标记赛事中即将开赛的比赛。
- 在**开赛前 10 分钟**向所有已订阅的会话（群聊/频道/私聊）广播推送卡片。
- 卡片内容：赛事名称、阶段、对阵战队、BO 赛制、开赛时间（北京时间）、比赛 ID 及 HLTV 详情链接。
- 内置去重机制与本地持久化状态，确保同一场比赛绝不重复触发提醒。

### 3.2 赛后延迟自动抓取战报与全员选手数据
- 比赛进入提醒时自动加入战报追踪队列。
- **赛制差异化延迟**：比赛开始后首次抓取战报的时间根据赛制差异化设定（用户可在配置中自定义）：
  - **BO1**：开赛后 `x` 分钟（默认 45 分钟）
  - **BO3**：开赛后 `y` 分钟（默认 120 分钟）
  - **BO5**：开赛后 `z` 分钟（默认 240 分钟）
- **未完赛重试轮询**：若到达首次时间时比赛仍在进行（如加时赛或网络暂停），自动按设定的重试间隔（默认 10 分钟）进行定期重试，直到比赛完赛或达到最大重试次数（默认 30 次）。
- **全员战绩呈现**：完赛战报必须包含双方全部 10 名选手的详细赛后表现，包括：
  - K-D（击杀-死亡）及 +/- 净胜差
  - ADR（局均伤害）
  - Rating 2.0 / 3.0
  - KAST（参团贡献率）

### 3.3 每日固定时间赛程定时推送
- 用户可配置每日推送时间点（默认 `09:00`，24 小时制 `HH:MM`）。
- 到达指定时间点自动汇总当日已标记赛事的赛程并群发推送。

### 3.4 比赛数据与单图选手详细查询 (`/hltv match`)
- 支持通过指令查询任意比赛（正在进行、已完赛、未开赛）的深度数据：
  - `/hltv match <ID或战队名>`：查询全场总比分、选图情况及双方全部选手的综合数据。
  - `/hltv match <ID或战队名> <图号>`：查询单图（如图 1、图 2）的比分、半场比分及专属单图全员 KDA/Rating 数据。

---

## 4. 关注赛事：手动标记（取代自动分级）

- **不做自动分级**：不再依据赛事名称关键词、星级或世界排名判断 T1/T2/T3。历史上的星级/T 级逻辑已删除，不得重新引入。
- **唯一推送依据**：配置项 `tracked_events`（HLTV 赛事 ID 列表）。赛前提醒、赛后战报、每日赛程、`/hltv today`、`/hltv results` 只处理这些赛事的比赛。
- **指令**：
  - `/hltv events [ongoing|upcoming|past]` 列出赛事与 ID（来自 `/api/v1/events`）。
  - `/hltv track <ID>`（管理员）校验 ID 存在（`/api/v1/events/{id}`）后写入配置并缓存赛事名。
  - `/hltv untrack <ID>`（管理员）移除。
  - `/hltv tracked` 查看列表。
- **判定方式**：比赛对象的 `event.id` 是否在 `tracked_events` 中。赛果列表本身没有 `event.id`，因此 `/hltv results` 与战报流程按赛事逐个调用 `/api/v1/results?event=<ID>`。
- **未标记任何赛事时**：后台任务直接跳过，查询指令提示先标记。

---

## 5. 全球多赛区动态时区识别与比赛日对齐

### 5.1 痛点与核心原则
- **拒绝固定单一时区**：全球 CS 赛事分布在欧洲、北美、南美、亚洲、大洋洲等不同地区，其当地时区各不相同，直接写死欧洲时区或中国时区均不合理。
- **跨天赛程完整性**：中国观众观看欧美比赛时，北京时间 23:00 与次日凌晨 00:00 / 01:30 的比赛在当地黄金时间属于同一轮比赛日，绝对不能被零点生硬切割为两天。

### 5.2 动态识别与处理机制
- **动态赛区与时区识别 (`get_match_timezone`)**：
  - 优先读取比赛数据的 `region` 字段（`Europe`, `Americas`, `North America`, `South America`, `Asia`, `Oceania`, `CIS` 等）。
  - 若无明确字段，深入解析赛事名称与主办城市关键词（如 Curitiba, Rio -> `America/Sao_Paulo`；Dallas, Atlanta -> `America/New_York`；Cologne, Katowice, Malta -> `Europe/Berlin`；Shanghai, Chengdu -> `Asia/Shanghai`；Melbourne, Sydney -> `Australia/Sydney` 等）。
  - 提供可配置的兜底时区 `matchday_timezone`（默认 `Europe/Berlin`，与 `_conf_schema.json` 键名一致）。
- **动态比赛日换算 (`get_matchday`)**：
  - 针对每场比赛，以其**主办地真实时区**换算当地比赛日（`YYYY-MM-DD`）。
- **双时区并存展示**：
  - 消息统一标明中国标准时间（CST，便于读者作息安排），同时在括号内附带比赛当地时间与时区缩写（例如 `[23:00 (当地 17:00 CEST)]` 或 `[次日 02:00 (当地 15:00 BRT)]`）。

### 5.3 赛程与战报自动顺延
- **今日赛程 (`/hltv today`)**：
  - 展示属于当天比赛日的关注赛事比赛。
  - **若当天的比赛已全部打完，自动顺延展示次日（下一比赛日）的赛程预告**。
- **近期战报 (`/hltv results`)**：
  - 优先展示关注赛事当天已完赛的赛果。
  - 若今日赛事尚未开打或未完赛，展示最近一个已完赛比赛日的全部战报，并自动提示今日首场比赛的开战时间与对阵。

---

## 6. 指令系统一览表

| 指令 | 别名 | 功能说明 |
| :--- | :--- | :--- |
| `/hltv events [ongoing/upcoming/past]` | `/hltv 赛事` | 列出赛事及 ID，已标记带 ✅ |
| `/hltv track <赛事ID>` | `/hltv 标记`、`/hltv 关注` | 标记关注赛事（管理员） |
| `/hltv untrack <赛事ID>` | `/hltv 取消标记`、`/hltv 取消关注` | 取消关注（管理员） |
| `/hltv tracked` | `/hltv 已标记`、`/hltv 关注列表` | 查看关注赛事 |
| `/hltv today` | `/hltv 赛程`、`/hltv 今日赛程` | 关注赛事今日赛程（当日赛毕自动顺延明日） |
| `/hltv results` | `/hltv 战报`、`/hltv 最近战报` | 关注赛事近期赛果（按当地比赛日） |
| `/hltv live` | `/hltv 正在进行`、`/hltv 实时` | 正在进行的比赛，关注赛事标 ⭐ |
| `/hltv match <ID/战队> [图号]` | `/hltv 比赛`、`/hltv 数据` | 比赛全场/单图全员数据 |
| `/hltv sub` / `/hltv unsub` | `/hltv 订阅` / `/hltv 取消订阅` | 订阅管理 |
| `/hltv status` | `/hltv 状态` | 运行状态 |
| `/hltv help` | | 帮助 |

---

## 7. 配置项规范 (`_conf_schema.json`)

1. `api_base` (string): API 地址（默认 `https://hltv.rinyin.top`）
2. `server_ip` (string): 可选直连 IP（默认空）
3. `matchday_timezone` (string): 兜底比赛日时区（默认 `Europe/Berlin`）
4. `notify_targets` (list): 推送目标会话
5. `tracked_events` (list): 关注赛事 ID 列表
6. `match_reminder_enabled` / `result_report_enabled` / `daily_schedule_enabled` (bool)
7. `bo1_delay_minutes` / `bo3_delay_minutes` / `bo5_delay_minutes` (int): 45 / 120 / 240
8. `result_retry_interval` (int): 10；`max_result_retries` (int): 30
9. `daily_schedule_time` (string): `09:00`
10. `timezone` (string): `Asia/Shanghai`

代码中读取的键名必须与此一致；不得引用 schema 中不存在的键。

---

## 8. 版本控制与代码托管规范

- **托管平台**：GitHub
- **仓库地址**：`https://github.com/Rinyin/astrbot_plugin_hltv`
- **公开仓库**：仓库为 Public。**严禁提交任何敏感信息**：服务器 IP、SSH 账号/密钥路径、部署路径、个人邮箱等一律不得出现在代码、文档或提交信息中。
- **.gitignore 过滤项**：必须忽略 `__pycache__/`, `*.pyc`, `*.zip`, `*.log`, `.venv/` 等临时文件。运行状态文件不在插件目录内，无需忽略。

---

## 9. AstrBot 插件开发准则（强制）

- 插件主类位于 `main.py`，继承 `Star`，必须使用 `@register(...)` 装饰器注册；不得依赖按类名猜测的旧版加载方式。
- 仅从 `astrbot.api` 及其子包导入框架符号（`logger`、`AstrBotConfig`、`MessageChain`、`StarTools` 等），不得直接引用 `astrbot.core.*` 私有路径。
- 日志统一使用 `from astrbot.api import logger`，捕获异常时记录 `exc_info=True`，禁止 `except Exception: pass` 静默吞错。
- 持久化数据（运行状态等）必须写入 `StarTools.get_data_dir()` 返回的 `data/plugin_data/astrbot_plugin_hltv/` 目录，不得写入 `data/` 根目录或插件目录。
- 配置只通过 `_conf_schema.json` 声明，代码中读取的配置键名必须与 schema 完全一致。
- 每次提交前必须运行 `ruff check` 与 `ruff format`，且通过。
- `metadata.yaml` 与 `@register` 的版本号保持同步。

