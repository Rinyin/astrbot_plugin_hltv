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
- **后端服务器基础设施**：
  - 服务器信息：`example.host`
  - SSH 认证密钥：`(redacted)`
- **网络防污染与直连机制**：
  - Windows 环境下常存在 TUN/Clash 等代理软件接管流量（产生 Fake-IP，导致 TLS 握手异常或 DNS 污染）。
  - 插件内置 `HostResolver`，将 `hltv.rinyin.top` 强行绑定直连至真实后端 IP `x.x.x.x`，并在发生网络抖动或超时（15s）时具备完善的容错处理。

---

## 3. 核心功能与自动化调度需求

### 3.1 赛前 10 分钟自动预警提醒
- 自动持续轮询即将开赛的大型/焦点比赛（T1/T2 级别）。
- 在**开赛前 10 分钟**向所有已订阅的会话（群聊/频道/私聊）广播推送卡片。
- 卡片内容：赛事名称、阶段、对阵战队、赛事级别（T1/T2）、BO 赛制、开赛时间（北京时间）、比赛 ID 及 HLTV 详情链接。
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
- 到达指定时间点自动汇总当日符合级别要求的所有大型赛事赛程并群发推送。

### 3.4 比赛数据与单图选手详细查询 (`/hltv match`)
- 支持通过指令查询任意比赛（正在进行、已完赛、未开赛）的深度数据：
  - `/hltv match <ID或战队名>`：查询全场总比分、选图情况及双方全部选手的综合数据。
  - `/hltv match <ID或战队名> <图号>`：查询单图（如图 1、图 2）的比分、半场比分及专属单图全员 KDA/Rating 数据。

---

## 4. 赛事分级与过滤体系（去除星级，直标 T 级）

- **取消星级显示**：彻底移除所有界面与消息中的星级符号（`⭐`），直接展示赛事级别：`[T1 赛事]` 或 `[T2 赛事]`。
- **过滤低级赛事**：彻底过滤 T3 级别的小型比赛（如各类公开预选赛、未达标网吧赛、次级小联赛），保证信息流干净。
- **级别判定规则**：
  - **T1 精英赛事**：
    - 赛事名称包含 Major, IEM, ESL Pro League, BLAST, StarLadder, EWC (Esports World Cup), PGL, BetBoom Dacha 等顶级世界大赛。
    - 或 HLTV 官方标记为 3 星及以上。
  - **T2 大型/焦点赛事**：
    - 赛事名称包含 Thunderpick, Skyesports, YaLLa, FireLeague, Logitech, ESL Challenger, CCT Global 等。
    - 或对阵双方中至少有一支属于 **HLTV 世界排名前 30 强队**（插件自动向 `/api/v1/rankings/teams` 请求并缓存世界 Top 30 战队名单）。
    - 或 HLTV 官方标记为 1~2 星。
  - **T3 次级赛事**：不符合 T1/T2 条件的其余比赛，全部予以忽略，不推提醒亦不入焦点赛程。

---

## 5. 全球多赛区动态时区识别与比赛日对齐

### 5.1 痛点与核心原则
- **拒绝固定单一时区**：全球 CS 赛事分布在欧洲、北美、南美、亚洲、大洋洲等不同地区，其当地时区各不相同，直接写死欧洲时区或中国时区均不合理。
- **跨天赛程完整性**：中国观众观看欧美比赛时，北京时间 23:00 与次日凌晨 00:00 / 01:30 的比赛在当地黄金时间属于同一轮比赛日，绝对不能被零点生硬切割为两天。

### 5.2 动态识别与处理机制
- **动态赛区与时区识别 (`get_match_timezone`)**：
  - 优先读取比赛数据的 `region` 字段（`Europe`, `Americas`, `North America`, `South America`, `Asia`, `Oceania`, `CIS` 等）。
  - 若无明确字段，深入解析赛事名称与主办城市关键词（如 Curitiba, Rio -> `America/Sao_Paulo`；Dallas, Atlanta -> `America/New_York`；Cologne, Katowice, Malta -> `Europe/Berlin`；Shanghai, Chengdu -> `Asia/Shanghai`；Melbourne, Sydney -> `Australia/Sydney` 等）。
  - 提供可配置的兜底时区 `default_matchday_timezone`（默认 `Europe/Berlin`）。
- **动态比赛日换算 (`get_matchday`)**：
  - 针对每场比赛，以其**主办地真实时区**换算当地比赛日（`YYYY-MM-DD`）。
- **双时区并存展示**：
  - 消息统一标明中国标准时间（CST，便于读者作息安排），同时在括号内附带比赛当地时间与时区缩写（例如 `[23:00 (当地 17:00 CEST)]` 或 `[次日 02:00 (当地 15:00 BRT)]`）。

### 5.3 赛程与战报自动顺延
- **今日赛程 (`/hltv today`)**：
  - 展示属于当天比赛日的大型赛事。
  - **若当天的比赛已全部打完，自动顺延展示次日（下一比赛日）的赛程预告**。
- **近期战报 (`/hltv results`)**：
  - 优先展示当天已完赛的全部大型赛果。
  - 若今日赛事尚未开打或未完赛，展示最近一个已完赛比赛日的全部战报，并自动提示今日首场比赛的开战时间与对阵。

---

## 6. 指令系统一览表

| 指令 | 别名 | 功能说明 |
| :--- | :--- | :--- |
| `/hltv match <ID或战队> [图号]` | `/hltv 比赛`、`/hltv 数据`、`/hltv 战报详情` | 查看比赛全员 KDA、Rating、ADR、KAST 及单图详细表现 |
| `/hltv today` | `/hltv 赛程`、`/hltv 今日赛程` | 查看今日大型赛事（当日赛毕自动顺延明日预告） |
| `/hltv results` | `/hltv 战报`、`/hltv 最近战报` | 查看当天大型/精英赛事完赛战报（按当地比赛日聚合） |
| `/hltv live` | `/hltv 正在进行`、`/hltv 实时` | 查看当前正在进行的比赛及实时大比分（含 T 级别） |
| `/hltv sub` | `/hltv 订阅` | 将当前会话加入推送列表（提醒、战报、赛程） |
| `/hltv unsub` | `/hltv 取消订阅` | 取消当前会话的订阅 |
| `/hltv status` | `/hltv 状态` | 查看插件运行状态、已订阅数量与各项配置 |
| `/hltv help` | `/hltv` | 查看插件帮助手册与使用说明 |

---

## 7. 配置项规范 (`_conf_schema.json`)

所有配置均接入 AstrBot WebUI，管理员可直接在线修改：
1. `api_base` (string): HLTV API 基础地址（默认 `https://hltv.rinyin.top`）
2. `server_ip` (string): API 服务器真实直连 IP（默认 `x.x.x.x`）
3. `matchday_timezone` (string): 比赛日兜底时区（默认 `Europe/Berlin`）
4. `min_stars` (int): 辅助星级门槛（默认 `1`）
5. `notify_targets` (list): 订阅推送的目标会话列表
6. `match_reminder_enabled` (bool): 是否开启赛前 10 分钟提醒（默认 `true`）
7. `result_report_enabled` (bool): 是否开启赛后战报推送（默认 `true`）
8. `bo1_delay_minutes` (int): BO1 赛后首次获取战报延迟分钟数（默认 `45`）
9. `bo3_delay_minutes` (int): BO3 赛后首次获取战报延迟分钟数（默认 `120`）
10. `bo5_delay_minutes` (int): BO5 赛后首次获取战报延迟分钟数（默认 `240`）
11. `result_retry_interval` (int): 战报未完赛重试间隔分钟数（默认 `10`）
12. `max_result_retries` (int): 战报最大重试次数（默认 `30`）
13. `daily_schedule_enabled` (bool): 是否开启每日赛程推送（默认 `true`）
14. `daily_schedule_time` (string): 每日赛程推送时间点（默认 `09:00`）
15. `timezone` (string): 本地显示时区（默认 `Asia/Shanghai`）

---

## 8. 版本控制与代码托管规范

- **托管平台**：GitHub
- **仓库地址**：`https://github.com/Rinyin/astrbot_plugin_hltv`
- **私有属性要求（绝对约束）**：**必须为私有仓库（Private）**，禁止设为公开。
- **.gitignore 过滤项**：必须忽略 `__pycache__/`, `*.pyc`, `*.zip`, `*.log`, `*state*.json`, `.venv/` 等临时与敏感运行文件。
