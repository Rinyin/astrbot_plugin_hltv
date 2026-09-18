# AstrBot HLTV 赛事提醒与战报播报插件 (`astrbot_plugin_hltv`)

基于 HLTV API 开发的 CS2 / CS:GO 焦点赛事自动追踪插件。支持大型比赛赛前 10 分钟自动预警提醒、赛后自动拉取详细比分战报及选手核心高光数据、每日固定时间推送当日赛程汇总，并支持通过聊天指令快捷管理群聊与私聊订阅。

---

## ✨ 核心特性

1. 📢 **开赛前 10 分钟自动预警提醒**
   - 自动扫描未来大型比赛（T1/T2 赛事），在开赛前 10 分钟向所有已订阅会话（群聊/频道/私聊）推送提醒卡片。
   - 包含赛事名称、阶段、对阵战队、赛事级别（T1/T2）、BO 赛制、开赛北京时间、比赛 ID 与 HLTV 详情链接。
   - 内置防重复提醒机制与持久化运行状态记录。

2. 🏆 **赛事智能评级与无星级展示**
   - **T1 精英赛事**：包含 Major、IEM、ESL Pro League、BLAST Premier、StarLadder、EWC、PGL、BetBoom Dacha 等。
   - **T2 大型赛事**：包含 Thunderpick、Skyesports、YaLLa、FireLeague、Logitech、ESL Challenger、CCT Global 等，或对阵双方包含 HLTV 世界排名前 30 强队。
   - **T3 彻底过滤**：自动过滤无名网吧赛与各次级公开预选赛，保持群内信息整洁高质。
   - 直接标注 `[T1 赛事]` 或 `[T2 赛事]`，不使用星级符号。

3. 🌍 **全球赛区当地时区智能对齐与赛程顺延**
   - **全球赛区动态识别**：智能检测各比赛与赛事的所属地区及主办城市（涵盖欧洲、北美、南美、中国/亚洲、大洋洲等），自动匹配比赛真实当地时区（如 `America/Sao_Paulo`、`America/New_York`、`Europe/Berlin`、`Asia/Shanghai`）。
   - **完美解决深夜场跨天**：北京时间 23:00 与次日凌晨 00:00/01:30 等黄金档比赛，均在其当地所属自然比赛日中完整聚合，杜绝粗暴按零点切断。
   - **赛程自动顺延**：查询今日赛程（`/hltv today`）时，若当天比赛已全部完赛，自动顺延展示次日预告。
   - **完赛赛果**：查询战报（`/hltv results`）时展示当前比赛日已完赛记录；若今日比赛尚未打完，展示最近完赛日战报并提示今日首场比赛开打时间。

4. 🏁 **赛后延迟自动抓取战报与全员数据**
   - 赛前提醒时自动登记入战报追踪队列。
   - **赛制差异化延迟**：支持为 BO1、BO3、BO5 分别配置比赛开始后的首次查询延迟时间（例如 BO1 45 分钟、BO3 120 分钟、BO5 240 分钟）。
   - **智能轮询重试**：若首次到达时间时比赛仍在进行（如加时赛或网络推迟），自动按配置的重试间隔（默认每 10 分钟）进行轮询，直至完赛。
   - **全员战绩呈现**：完赛战报分队推送双方所有 10 名选手的完整战绩（K-D、+/- 净胜、ADR、Rating、KAST）。

5. 🔍 **按比赛ID/战队查看指定比赛与单图全员数据**
   - 支持使用指令指定查看任意比赛（无论是已完赛、正在进行还是未开始）。
   - 支持查看全场综合统计或按图号查看各单图（图1、图2、图3等）选手 KDA、ADR、Rating 等详细表现。

6. 📅 **每日固定时间赛程推送**
   - 用户可自定义每日推送时间点（默认 `09:00`）与显示时区（默认 `Asia/Shanghai`）。
   - 每天到达设定时间自动整理汇总当日所有大型比赛列表并群发，附带比赛 ID 便于直接查询。

7. 🌐 **网络直连与防污染优化**
   - 内置 `HostResolver` 自定义解析器，直接绑定服务端真实 IP（`x.x.x.x`），彻底规避 Windows 下由于 TUN/Clash 等代理软件 Fake-IP 分流导致的 TLS 握手失败与 DNS 污染问题。

8. 🐲 **战队中文别名库与陈旧缓存自愈**
   - **40+ 知名战队中文外号与缩写**：完美支持绿龙/TS(Spirit)、小蜜蜂(Vitality)、银河战舰/大表哥(FaZe)、老鼠(MOUZ)、A队(Astralis)、蒙古(The MongolZ)、天禄(TYLOO)、大狗/VP(Virtus.pro)、RA(Rare Atom)、LVG 等。
   - **半场陈旧缓存自动对齐**：若详情接口返回未完赛/半场旧数据，自动交叉检索最新完赛结果库（`get_results`）对齐最终大比分与完赛状态。
   - **比赛 ID 检索多级容灾**：当单场详情受 Cloudflare 官方限流 (503) 时，自动回退至赛果库与赛程库匹配，确保 ID 查比赛永不落空。
   - **手动与强制刷新**：提供 `/hltv refresh` 指令主动清除服务端缓存；`/hltv match <战队/ID> -r` 支持强制刷新。

---

## 🎮 指令菜单

在群聊或私聊中发送以下指令即可：

| 指令 | 别名 | 说明 |
| :--- | :--- | :--- |
| `/hltv match <ID/战队/别名> [图号] [-r]` | `/hltv 比赛`、`/hltv 数据`、`/hltv 战报详情` | 查看比赛全员 KDA、Rating、单图数据（支持绿龙、小蜜蜂等别名；加 -r 强刷） |
| `/hltv refresh` | `/hltv 刷新`、`/hltv 清除缓存` | 主动清除服务端 API 缓存，强制拉取最新赛程与比分 |
| `/hltv today` | `/hltv 赛程`、`/hltv 今日赛程` | 查看今日大型赛事（当日赛毕自动顺延明日预告） |
| `/hltv results` | `/hltv 战报`、`/hltv 最近战报` | 查看当天大型/精英赛事完赛战报（按比赛日时区） |
| `/hltv live` | `/hltv 正在进行`、`/hltv 实时` | 查看当前正在进行的比赛及实时大比分（含比赛ID与级别） |
| `/hltv sub` | `/hltv 订阅` | 将当前会话（群聊/私聊）加入推送目标列表 |
| `/hltv unsub` | `/hltv 取消订阅` | 将当前会话移出推送目标列表 |
| `/hltv status` | `/hltv 状态` | 查看插件运行状态、已订阅数量、比赛日时区与级别配置 |
| `/hltv help` | `/hltv` | 查看帮助菜单与指令说明 |

### 💡 比赛数据查询示例
- `/hltv match 2398026`：查看比赛 ID 为 2398026 的全场大比分、各图战况及双方全部选手战绩
- `/hltv match 2398026 1`：查看图 1（例如 Inferno）的双方选图、半场比分及图 1 专属全员 KDA/Rating
- `/hltv match 绿龙`：通过战队中文别名快速查询 Spirit 最新比赛数据
- `/hltv match 2398026 -r`：强制清除服务端缓存并获取最新即时数据
- `/hltv refresh`：一键清除服务端缓存，同步最新全量赛事比分

---

## ⚙️ 可视化配置参数说明

所有配置项均已接入 AstrBot WebUI 配置面板，管理员可在网页端直接可视化修改并即时保存生效：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `api_base` | string | `https://hltv.rinyin.top` | HLTV API 接口服务地址 |
| `server_ip` | string | `x.x.x.x` | 服务端真实 IP（用于防 DNS 污染/假 IP，直连服务器） |
| `matchday_timezone` | string | `Europe/Berlin` | 比赛日划分时区（欧洲当地时间，北京时间23点与次日凌晨归属同一比赛日） |
| `min_stars` | int | `1` | 焦点赛事辅助星级门槛 |
| `notify_targets` | list | `[]` | 接收提醒与赛程推送的目标会话列表（可通过 `/hltv sub` 快捷添加） |
| `match_reminder_enabled` | bool | `true` | 是否开启赛前 10 分钟提醒 |
| `result_report_enabled` | bool | `true` | 是否开启赛后战报推送 |
| `bo1_delay_minutes` | int | `45` | BO1 比赛开始后首次尝试获取战报的延迟时间（分钟） |
| `bo3_delay_minutes` | int | `120` | BO3 比赛开始后首次尝试获取战报的延迟时间（分钟） |
| `bo5_delay_minutes` | int | `240` | BO5 比赛开始后首次尝试获取战报的延迟时间（分钟） |
| `result_retry_interval` | int | `10` | 若比赛未完赛，后续重试查询战报的间隔（分钟） |
| `max_result_retries` | int | `30` | 战报查询最大重试次数（达到后自动停止追踪） |
| `daily_schedule_enabled` | bool | `true` | 是否开启每日赛程定时推送 |
| `daily_schedule_time` | string | `09:00` | 每日赛程推送时间点（24 小时制 `HH:MM`） |
| `timezone` | string | `Asia/Shanghai` | 本地显示时区名称（北京时间） |

---

## 📁 目录结构

```
astrbot_plugin_hltv/
├── README.md            # 插件使用与开发说明文档
├── metadata.yaml        # 插件元数据（名称、版本、作者、描述）
├── _conf_schema.json    # AstrBot WebUI 可视化配置 Schema
├── requirements.txt     # Python 依赖
├── api.py               # 异步 HLTV API 客户端（含 IP 直连与异常处理）
├── scheduler.py         # 赛前提醒、战报追踪轮询与每日推送调度器
└── main.py              # AstrBot 插件入口类（Star 子类与指令处理）
```
