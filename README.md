# AstrBot HLTV 赛事提醒与战报播报插件 (`astrbot_plugin_hltv`)

基于自建 HLTV API 的 CS2 赛事追踪插件。管理员用指令**手动标记**要关注的赛事，插件只对这些赛事推送开赛前 10 分钟提醒、赛后全员数据战报和每日赛程汇总，不做任何自动分级。

---

## ✨ 功能

1. **手动标记关注赛事** — `/hltv events` 列出 HLTV 当前/即将进行的赛事及 ID，`/hltv track <ID>` 标记，`/hltv untrack <ID>` 取消。只有被标记的赛事会触发推送。
2. **开赛前 10 分钟提醒** — 向所有订阅会话推送：赛事、阶段、对阵、赛制、北京时间、比赛 ID、HLTV 链接。开赛前 70 分钟后台预热该场图片，提醒前有界补齐。内置去重与持久化状态。
3. **赛后战报** — 按赛制延迟（BO1/BO3/BO5 可配置）拉取完赛详情，未完赛按间隔重试；推送大比分、逐图比分与半场、双方全部选手 K-D / +/- / ADR / Rating / KAST。
4. **每日赛程推送** — 每天固定时间汇总当日关注赛事的比赛；推送前 1 小时后台预下载当日战队与选手图片。
5. **查询指令** — 今日赛程（打完自动切明日）、近期赛果、正在进行的比赛、任意比赛全场/单图数据。
6. **全球赛区时区对齐** — 按比赛所在赛区/城市换算当地比赛日，北京时间深夜场与次日凌晨场归入同一比赛日；消息同时显示北京时间与当地时间。
7. **可选直连** — 配置 `server_ip` 后，自定义解析器把 API 域名固定解析到该 IP，规避本机代理 Fake-IP 导致的 TLS 失败。
8. **图片战报与海报** — 战报展示双方全员头像和统计；赛程按赛事分页，配合队标呈现对阵。比赛查询、自动战报、提醒和赛程共用图片输出，管理命令仍为文本。

## 图片渲染与安装

默认开启图片输出，自动依次尝试本地 Playwright/Chromium、AstrBot 的 `html_render` T2I 服务和文本。缺少浏览器或 T2I 不可用时仍可获取文本信息。`render_backend=local` 或 `t2i` 可指定图片后端，失败后回退文本；`image_enabled=false` 直接使用文本。

在运行 AstrBot 的 Python 环境中，进入本插件目录安装依赖和浏览器：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Linux 如提示浏览器缺少系统库，可按 Playwright 提示安装依赖。重载插件后使用 `/hltv match <比赛ID>` 或 `/hltv today` 验证图片。T2I 依赖 AstrBot 配置及渲染服务可用性；图片也需要平台适配器支持。

模板随附 Noto Sans SC 中文字体，按 SIL Open Font License 使用，许可证见 `templates/fonts/OFL.txt`。

队标、选手定妆照等素材长期保存，默认 **90 天检查更新**，这不是过期删除期限。更新时优先使用旧图，新图下载失败仍保留旧图；同一实体的素材 URL 变化会触发更新。默认素材缓存上限 **1GB**，超出容量后才按使用情况清理，避免每次发战报都重新下载。不完整素材使用占位图。成品战报与素材分开管理，待投递图片保留至任务结束，成功后的成品图片保留二十四小时。

首次请求只等待本地已缓存素材；缺失图片后台下载，暂用占位图，后续页面或查询复用缓存。手动查询逐页渲染发送，不等待整组图片完成。赛程按各赛事当地日期选择，跨日进行中的比赛始终保留并优先显示。队标使用浅色衬底，避免与深色卡片融合。Rating ≤0.95 为红色，0.95 至 1.05 之间为白色，≥1.05 为绿色；K-D 净胜差正数绿色、负数红色、零白色。

每日赛程推送前 1 小时，后台会预热该次赛程中关注赛事的战队与首发选手图片。例如 `09:00` 推送会在 `08:00` 开始，`00:30` 推送会在前一天 `23:30` 开始。赛前列表没有选手头像时，从参赛队伍名单获取，只处理首发或未标注状态的选手，跳过替补。

除每日推送预热外，每场已标记比赛在开赛前 70 分钟（即提醒前 1 小时）也会检查该场所需图片，缺失则后台补齐。它与每日预热共用素材收集、战队名单获取、缓存与单并发限速，只处理进入 70 分钟窗口的比赛，不会预下载所有未来赛程；完成状态按开赛时间持久化，重启后不重复请求，赛程改期会重新校验。失败按素材自身的 30 分钟失败冷却重试，并由提醒前的 70→10 分钟窗口自然收敛，不会在提醒前堆积无谓请求。该预热独立于 `daily_schedule_enabled`，仅随 `match_reminder_enabled` 与 `image_enabled` 启用；等待共享预热队列期间会再次确认比赛仍未取消关注、仍未进入提醒窗口，避免排队后为已失效的比赛发起请求。插件晚启动时按当前时间重新判定窗口，已进入提醒或开打的比赛不再预热。

预热跨场次去重，名单和图片请求均为单并发、间隔 1 秒，每轮最多查询 60 支队伍、处理 300 项图片。复用现有缓存及本地素材；名单获取或图片下载失败时，至少间隔 10 分钟重试，并保留图片缓存原有的 30 分钟失败冷却。成功名单在本次推送日期内存复用，完成日期会持久化。预热不阻塞推送与前台查询，插件卸载时清理任务；上游暂时缺图或持续下载失败时仍会使用占位图。

自动消息记录各目标、各页面的投递进度；重试和重启恢复只处理未成功部分。图片发送明确失败时尝试文本。重试间隔和次数沿用战报重试配置，失败任务可在 `/hltv status` 查看。平台无法确认的发送超时仍可能产生重复，不能保证严格的恰好一次投递。

### 图片获取与本地素材

插件优先通过同一个 `api_base` 的 `/api/v1/assets?url=<原始素材URL>` 获取图片，复用 API 服务器已有的 HLTV 会话；图片随后缓存在插件本地，模板只读取本地素材。HLTV 主站页面本身也引用图片 CDN，不能通过简单替换域名保证绕过 CDN。旧版 API 返回 404 时才尝试原始地址；临时服务失败时保留旧图，不反复访问被拦截的 CDN。

如希望自行准备图片，可在插件数据目录放置以下文件（HLTV 数字 ID 可从比赛详情或对应主站页面取得）：

```text
data/plugin_data/astrbot_plugin_hltv/assets/local/
├── team/11283.png       # 队标
├── player/429.webp      # 选手照片
└── event/8244.jpg       # 赛事背景或标志
```

支持 PNG、JPEG、WebP、SVG，单文件不超过 5MB。用户素材优先于 API 和自动缓存，不受 90 天更新及缓存容量清理影响；替换后下次渲染即生效。每个 ID 只保留一份图片。将 `assets/local/` 文件夹复制到另一台 AstrBot 的对应插件数据目录即可迁移，不需要复制配置、Cookie 或投递状态。自动缓存也使用相对文件名，可在停止插件后整体复制 `assets/`。

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
| `server_ip` | string | 空 | 可选，API 服务器直连 IP（绕过本机代理 Fake-IP / DNS 污染） |
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
| `daily_asset_prefetch_enabled` | bool | `true` | 每日赛程推送前 1 小时后台预下载当日关注赛事战队/选手图片 |
| `timezone` | string | `Asia/Shanghai` | 显示时区 |
| `image_enabled` | bool | `true` | 比赛消息使用图片，关闭后使用文本 |
| `render_backend` | string | `auto` | `auto`：本地 → T2I → 文本；`local` / `t2i`：指定后端，失败转文本 |
| `asset_refresh_days` | int | `90` | 素材检查更新周期，不是文件保留期限 |
| `asset_cache_max_mb` | int | `1024` | 长期素材缓存容量上限（MB） |

赛前单场图片预热复用上述缓存相关配置，无需单独开关，随 `match_reminder_enabled` 与 `image_enabled` 启用，且不受 `daily_schedule_enabled` 影响；`daily_asset_prefetch_enabled` 只控制每日推送预热。

运行状态（已提醒/追踪中/赛事名缓存、赛前预热完成记录）保存在 `data/plugin_data/astrbot_plugin_hltv/state.json`。

---

## 📁 目录结构

```
astrbot_plugin_hltv/
├── main.py              # 插件入口：@register 的 Star 子类与 /hltv 指令组
├── scheduler.py         # 后台轮询：赛前提醒、战报追踪、每日推送；消息格式化
├── api.py               # 异步 API 客户端（IP 直连、错误处理）
├── presentation.py      # 统一展示模型、分页及本地/T2I 渲染
├── assets.py            # 长期素材缓存与后台更新
├── delivery.py          # 持久化投递队列与分页重试
├── templates/           # 图片模板及中文字体
├── tests/               # 缓存、展示、命令及投递测试
├── _conf_schema.json    # WebUI 配置 Schema
├── metadata.yaml        # 插件元数据
├── requirements.txt     # 依赖
└── AGENT.md             # 需求与开发规范
```

## 上游 API 依赖

插件依赖一个将 hltv.org 页面转为 JSON 的自建 HLTV API（FastAPI）。本插件使用的端点：`/api/v1/matches`、`/api/v1/matches/{id}`、`/api/v1/results?event=`、`/api/v1/events`、`/api/v1/events/{id}`、`/api/v1/teams/{id}`，以及返回图片字节的 `/api/v1/assets?url=`。
