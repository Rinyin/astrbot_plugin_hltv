# AstrBot HLTV 赛事提醒与战报播报插件 (`astrbot_plugin_hltv`)

基于自建 HLTV API 的 CS2 赛事追踪插件。管理员用指令**手动标记**要关注的赛事，插件只对这些赛事推送开赛前 10 分钟提醒、赛后全员数据战报和每日赛程汇总，不做任何自动分级。

---

## ✨ 功能

1. **手动标记关注赛事** — `/hltv events` 列出 HLTV 当前/即将进行的赛事及 ID，`/hltv track <ID>` 标记，`/hltv untrack <ID>` 取消。只有被标记的赛事会触发推送。
2. **开赛前 10 分钟提醒** — 向所有订阅会话推送：赛事、阶段、对阵、赛制、北京时间、比赛 ID、HLTV 链接。内置去重与持久化状态。
3. **赛后战报** — 按赛制延迟（BO1/BO3/BO5 可配置）拉取完赛详情，未完赛按间隔重试；推送大比分、逐图比分与半场、双方全部选手 K-D / +/- / ADR / Rating / KAST。
4. **每日赛程推送** — 每天固定时间汇总当日关注赛事的比赛。
5. **查询指令** — 今日赛程（打完自动切明日）、近期赛果、正在进行的比赛、任意比赛全场/单图数据。
6. **全球赛区时区对齐** — 按比赛所在赛区/城市换算当地比赛日，北京时间深夜场与次日凌晨场归入同一比赛日；消息同时显示北京时间与当地时间。
7. **网络直连** — 自定义解析器把 API 域名直接绑定到服务器 IP，规避本机代理 Fake-IP 导致的 TLS 失败。

---

## 🎮 指令

| 指令 | 别名 | 说明 |
| :--- | :--- | :--- |
| `/hltv events [ongoing\|upcoming\|past]` | `/hltv 赛事` | 列出赛事及 ID，已标记的带 ✅ |
| `/hltv track <赛事ID>` | `/hltv 标记`、`/hltv 关注` | 标记关注赛事（管理员） |
| `/hltv untrack <赛事ID>` | `/hltv 取消标记`、`/hltv 取消关注` | 取消关注（管理员） |
| `/hltv tracked` | `/hltv 已标记`、`/hltv 关注列表` | 查看当前关注赛事 |
| `/hltv today` | `/hltv 赛程`、`/hltv 今日赛程` | 关注赛事今日赛程（当日赛毕自动顺延明日） |
| `/hltv results` | `/hltv 战报`、`/hltv 最近战报` | 关注赛事近期完赛结果（按当地比赛日） |
| `/hltv live` | `/hltv 正在进行`、`/hltv 实时` | 当前正在进行的比赛，关注赛事标 ⭐ |
| `/hltv match <ID/战队> [图号]` | `/hltv 比赛`、`/hltv 数据` | 比赛全场或单图全员数据 |
| `/hltv sub` / `/hltv unsub` | `/hltv 订阅` / `/hltv 取消订阅` | 本会话加入/移出推送列表 |
| `/hltv status` | `/hltv 状态` | 运行状态、关注赛事与配置 |
| `/hltv help` | | 帮助 |

### 典型流程

```
/hltv events            # 看进行中的赛事，记下 ID
/hltv track 8057        # 标记 StarLadder StarSeries Fall 2026
/hltv sub               # 本群订阅推送
/hltv today             # 查看今日关注赛程
/hltv match 2398108 2   # 查看某场比赛第 2 图全员数据
```

---

## ⚙️ 配置 (`_conf_schema.json`)

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `api_base` | string | `https://hltv.rinyin.top` | HLTV API 地址 |
| `server_ip` | string | `x.x.x.x` | API 服务器直连 IP |
| `matchday_timezone` | string | `Europe/Berlin` | 无法识别赛区时的兜底比赛日时区 |
| `notify_targets` | list | `[]` | 推送目标会话（`/hltv sub` 自动维护） |
| `tracked_events` | list | `[]` | 关注赛事 ID（`/hltv track` 自动维护） |
| `match_reminder_enabled` | bool | `true` | 赛前 10 分钟提醒 |
| `result_report_enabled` | bool | `true` | 赛后战报推送 |
| `bo1_delay_minutes` / `bo3_delay_minutes` / `bo5_delay_minutes` | int | `45` / `120` / `240` | 各赛制开赛后首次拉战报的延迟 |
| `result_retry_interval` | int | `10` | 未完赛重试间隔（分钟） |
| `max_result_retries` | int | `30` | 最大重试次数 |
| `daily_schedule_enabled` | bool | `true` | 每日赛程推送 |
| `daily_schedule_time` | string | `09:00` | 推送时间（HH:MM） |
| `timezone` | string | `Asia/Shanghai` | 显示时区 |

运行状态（已提醒/追踪中/赛事名缓存）保存在 `data/plugin_data/astrbot_plugin_hltv/state.json`。

---

## 📁 目录结构

```
astrbot_plugin_hltv/
├── main.py              # 插件入口：@register 的 Star 子类与 /hltv 指令组
├── scheduler.py         # 后台轮询：赛前提醒、战报追踪、每日推送；消息格式化
├── api.py               # 异步 API 客户端（IP 直连、错误处理）
├── _conf_schema.json    # WebUI 配置 Schema
├── metadata.yaml        # 插件元数据
├── requirements.txt     # 依赖
└── AGENT.md             # 需求与开发规范
```

## 上游 API 依赖

插件依赖自建的 HLTV API（FastAPI，服务器 `~/hltv-api`）。本插件使用的端点：`/api/v1/matches`、`/api/v1/matches/{id}`、`/api/v1/results?event=`、`/api/v1/events`、`/api/v1/events/{id}`。
