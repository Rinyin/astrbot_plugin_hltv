from datetime import datetime
from typing import Any, Dict, List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .api import HLTVClient
from .scheduler import HLTVScheduler

PLUGIN_NAME = "astrbot_plugin_hltv"


@register(
    PLUGIN_NAME,
    "Rinyi",
    "HLTV CS2 关注赛事赛前10分钟提醒、赛后战报获取与每日赛程推送",
    "1.3.0",
    "https://github.com/Rinyi/astrbot_plugin_hltv",
)
class HLTVPlugin(Star):
    """HLTV CS2 赛事自动提醒、战报获取与每日赛程推送插件。

    推送范围由管理员通过 /hltv track <赛事ID> 手动标记，不做自动分级。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 确保列表型配置存在
        for key in ("notify_targets", "tracked_events"):
            if key not in self.config:
                self.config[key] = []

        api_base = self.config.get("api_base", "https://hltv.rinyin.top")
        server_ip = (self.config.get("server_ip") or "").strip()
        self.client = HLTVClient(base_url=api_base, server_ip=server_ip)
        self.scheduler = HLTVScheduler(
            context=self.context,
            config=self.config,
            client=self.client,
            plugin_name=PLUGIN_NAME,
        )

    async def initialize(self):
        """当插件被载入或启动时调用"""
        self.scheduler.start()
        logger.info("[HLTV] HLTV 赛事推送插件已成功初始化！")

    async def terminate(self):
        """当插件被禁用或重载时调用"""
        await self.scheduler.stop()
        await self.client.close()
        logger.info("[HLTV] HLTV 赛事推送插件已终止。")

    def _save_config(self):
        """保存配置更改到 data/config/<plugin>_config.json"""
        try:
            self.config.save_config()
        except Exception as e:
            logger.error(f"[HLTV] 保存配置文件失败: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _collect_tracked_matches(
        self, days: int = 5
    ) -> Dict[str, List[Dict[str, Any]]]:
        """拉取未来 N 天赛程，仅保留已标记赛事，按当地比赛日聚合"""
        all_matches = await self.client.get_matches(status="all", days=days, limit=300)
        by_matchday: Dict[str, List[Dict[str, Any]]] = {}
        for m in all_matches:
            if not self.scheduler.is_tracked_match(m):
                continue
            ts = self.scheduler.parse_starts_at_ts(m)
            if ts:
                by_matchday.setdefault(self.scheduler.get_matchday(m, ts), []).append(m)
        for day_matches in by_matchday.values():
            day_matches.sort(key=lambda x: self.scheduler.parse_starts_at_ts(x) or 0)
        return by_matchday

    async def _collect_tracked_results(self, days: int = 7) -> List[Dict[str, Any]]:
        """逐个已标记赛事拉取完赛记录（赛果列表本身不带赛事 ID，需按赛事查询）"""
        results: List[Dict[str, Any]] = []
        seen: set = set()
        for event_id in self.scheduler.get_tracked_event_ids():
            try:
                event_results = await self.client.get_results(
                    days=days, limit=100, event_id=event_id
                )
            except Exception as e:
                logger.warning(
                    f"[HLTV] 拉取赛事 {event_id} 赛果失败: {e}", exc_info=True
                )
                continue
            for m in event_results:
                m_id = str(m.get("id") or "")
                if m_id and m_id not in seen:
                    seen.add(m_id)
                    results.append(m)
        return results

    @staticmethod
    def _event_line(ev: Dict[str, Any], tracked: bool) -> str:
        mark = "✅" if tracked else "▫️"
        date_text = ev.get("date_text") or ""
        date_str = f" · {date_text}" if date_text else ""
        etype = ev.get("event_type") or ""
        type_str = f" · {etype}" if etype else ""
        return f"{mark} {ev.get('id')}  {ev.get('name')}{date_str}{type_str}"

    # ------------------------------------------------------------------
    # 指令组: /hltv
    # ------------------------------------------------------------------

    @filter.command_group("hltv")
    def hltv(self):
        """HLTV 赛事查询与推送管理指令组"""
        pass

    @hltv.command("help")
    async def hltv_help(self, event: AstrMessageEvent):
        """查看 HLTV 插件帮助信息"""
        help_text = (
            "🎮 【HLTV 赛事助手指令菜单】\n"
            "------------------------------------\n"
            "📌 赛事标记（管理员）\n"
            "• /hltv events [upcoming]：列出进行中(或即将开始)的赛事及其ID\n"
            "• /hltv track <赛事ID>：标记关注赛事，仅关注赛事会推送\n"
            "• /hltv untrack <赛事ID>：取消关注\n"
            "• /hltv tracked：查看当前已标记的赛事\n"
            "------------------------------------\n"
            "📊 查询\n"
            "• /hltv today：查看关注赛事今日赛程（打完自动切换明日预告）\n"
            "• /hltv results：查看关注赛事完赛战报\n"
            "• /hltv live：查看当前正在进行的比赛\n"
            "• /hltv match <ID/战队> [图号]：查看比赛全员KDA/Rating及单图数据\n"
            "------------------------------------\n"
            "🔔 推送\n"
            "• /hltv sub / unsub：订阅或取消本会话的提醒、战报与每日赛程\n"
            "• /hltv status：查看插件运行状态\n"
        )
        yield event.plain_result(help_text)

    # ---------------- 赛事标记 ----------------

    @hltv.command("events", alias={"赛事", "赛事列表"})
    async def hltv_events(self, event: AstrMessageEvent, status: str = "ongoing"):
        """列出 HLTV 赛事及其 ID。用法: /hltv events [ongoing|upcoming|past]"""
        status = (status or "ongoing").strip().lower()
        aliases = {
            "进行中": "ongoing",
            "即将": "upcoming",
            "未来": "upcoming",
            "过去": "past",
            "已结束": "past",
        }
        status = aliases.get(status, status)
        if status not in ("ongoing", "upcoming", "past", "all"):
            yield event.plain_result(
                "ℹ️ 用法：/hltv events [ongoing|upcoming|past]（默认 ongoing）"
            )
            return

        events = await self.client.get_events(status=status, limit=40)
        if not events:
            yield event.plain_result("⚠️ 未获取到赛事列表，请稍后重试。")
            return

        tracked_ids = set(self.scheduler.get_tracked_event_ids())
        title = {
            "ongoing": "进行中的赛事",
            "upcoming": "即将开始的赛事",
            "past": "已结束的赛事",
            "all": "全部赛事",
        }[status]
        lines = [
            f"🏆 【HLTV {title}】（✅ 为已标记）",
            "------------------------------------",
        ]
        for ev in events:
            lines.append(self._event_line(ev, str(ev.get("id")) in tracked_ids))
        lines.append("------------------------------------")
        lines.append("💡 发送 /hltv track <赛事ID> 标记关注赛事")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @hltv.command("track", alias={"标记", "关注"})
    async def hltv_track(self, event: AstrMessageEvent, event_id: str = ""):
        """标记关注赛事（管理员）。用法: /hltv track <赛事ID>"""
        event_id = (event_id or "").strip()
        if not event_id.isdigit():
            yield event.plain_result(
                "ℹ️ 用法：/hltv track <赛事ID>\n💡 发送 /hltv events 查看赛事ID"
            )
            return

        tracked = self.scheduler.get_tracked_event_ids()
        if event_id in tracked:
            name = self.scheduler.event_display_name(event_id)
            yield event.plain_result(f"ℹ️ 赛事 {name} ({event_id}) 已在关注列表中。")
            return

        detail = await self.client.get_event(event_id)
        if not detail:
            yield event.plain_result(
                f"❌ 未找到 ID 为 {event_id} 的赛事，请通过 /hltv events 确认 ID。"
            )
            return

        name = detail.get("name") or f"赛事 {event_id}"
        tracked.append(event_id)
        self.config["tracked_events"] = tracked
        self._save_config()
        self.scheduler.remember_event_name(event_id, name)

        date_text = detail.get("date_text") or ""
        prize = detail.get("prize_pool") or ""
        extra = "\n".join(
            part
            for part in (
                f"📅 日期：{date_text}" if date_text else "",
                f"💰 奖金池：{prize}" if prize else "",
            )
            if part
        )
        yield event.plain_result(
            f"✅ 已标记关注赛事：{name} (ID {event_id})\n"
            + (extra + "\n" if extra else "")
            + f"当前共关注 {len(tracked)} 个赛事，将推送其赛前提醒、战报与每日赛程。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @hltv.command("untrack", alias={"取消标记", "取消关注"})
    async def hltv_untrack(self, event: AstrMessageEvent, event_id: str = ""):
        """取消关注赛事（管理员）。用法: /hltv untrack <赛事ID>"""
        event_id = (event_id or "").strip()
        tracked = self.scheduler.get_tracked_event_ids()
        if event_id not in tracked:
            yield event.plain_result(
                f"ℹ️ 赛事 {event_id or '(空)'} 不在关注列表中。发送 /hltv tracked 查看。"
            )
            return

        tracked.remove(event_id)
        self.config["tracked_events"] = tracked
        self._save_config()
        name = self.scheduler.event_display_name(event_id)
        yield event.plain_result(
            f"✅ 已取消关注：{name} (ID {event_id})，剩余 {len(tracked)} 个关注赛事。"
        )

    @hltv.command("tracked", alias={"已标记", "关注列表"})
    async def hltv_tracked(self, event: AstrMessageEvent):
        """查看当前已标记的关注赛事"""
        tracked = self.scheduler.get_tracked_event_ids()
        if not tracked:
            yield event.plain_result(
                "ℹ️ 尚未标记任何赛事，插件不会推送提醒或赛程。\n"
                "💡 发送 /hltv events 查看赛事，再用 /hltv track <ID> 标记。"
            )
            return
        lines = ["📌 【当前关注赛事】", "------------------------------------"]
        for event_id in tracked:
            lines.append(
                f"✅ {event_id}  {self.scheduler.event_display_name(event_id)}"
            )
        lines.append("------------------------------------")
        lines.append("💡 /hltv untrack <ID> 可取消关注")
        yield event.plain_result("\n".join(lines))

    # ---------------- 查询 ----------------

    @hltv.command("match", alias={"比赛", "数据", "战报详情", "比赛详情"})
    async def hltv_match(
        self, event: AstrMessageEvent, query: str = "", map_arg: str = ""
    ):
        """查看指定比赛的全员KDA、Rating及单图详细数据。用法: /hltv match <比赛ID或战队名称> [图号]"""
        clean_q = query.strip()
        if not clean_q:
            yield event.plain_result(
                "ℹ️ 请提供要查询的比赛ID或战队名称。\n"
                "💡 格式：/hltv match <比赛ID或战队名> [图号]\n"
                "例如：\n"
                "  • /hltv match 2398026 （查看全场全员KDA、Rating）\n"
                "  • /hltv match 2398026 1 （查看图1单图选手数据）\n"
                "  • /hltv match Spirit （按战队名查询比赛）\n"
                "提示：发送 /hltv live、/hltv results 或 /hltv today 可获取比赛ID。"
            )
            return

        detail = await self.client.find_match(clean_q)
        if not detail:
            yield event.plain_result(
                f"❌ 未找到与「{clean_q}」相关的比赛数据。\n提示：可使用数字比赛ID或更精确的战队名重新查询。"
            )
            return

        # 若 find_match 仅返回简略信息，且有 id，尝试获取全量 detail
        if (
            "player_stats" not in detail or not detail.get("player_stats")
        ) and detail.get("id"):
            full_detail = await self.client.get_match_detail(str(detail.get("id")))
            if full_detail:
                detail = full_detail

        msg = self.scheduler.format_match_detail(
            detail, map_query=map_arg.strip() if map_arg else None
        )
        yield event.plain_result(msg)

    @hltv.command("today", alias={"赛程", "今日赛程"})
    async def hltv_today(self, event: AstrMessageEvent):
        """查看关注赛事今日赛程（按当地比赛日）。若今日比赛均已完赛，自动顺延展示明日预告。"""
        if not self.scheduler.get_tracked_event_ids():
            yield event.plain_result(
                "ℹ️ 尚未标记任何关注赛事。发送 /hltv events 查看赛事，再用 /hltv track <ID> 标记。"
            )
            return

        by_matchday = await self._collect_tracked_matches(days=5)
        current_matchday = self.scheduler.get_current_matchday()
        today_matches = by_matchday.get(current_matchday, [])

        has_active = any(m.get("status") in ("upcoming", "live") for m in today_matches)

        if today_matches and has_active:
            msg = self.scheduler.format_daily_schedule(
                today_matches, current_matchday, is_next_day=False
            )
        else:
            future_days = sorted(d for d in by_matchday if d > current_matchday)
            if future_days:
                next_day = future_days[0]
                msg = self.scheduler.format_daily_schedule(
                    by_matchday[next_day], next_day, is_next_day=True
                )
            elif today_matches:
                msg = self.scheduler.format_daily_schedule(
                    today_matches, current_matchday, is_next_day=False
                )
            else:
                msg = (
                    f"📅 【HLTV 赛程预告】\n比赛日 {current_matchday} 及近期，"
                    "已标记的赛事没有比赛安排。"
                )

        yield event.plain_result(msg)

    @hltv.command("live", alias={"正在进行", "实时"})
    async def hltv_live(self, event: AstrMessageEvent):
        """查看当前正在进行的比赛（关注赛事标 ⭐）"""
        live_matches = await self.client.get_live_matches()

        if not live_matches:
            yield event.plain_result("🔴 当前暂无正在进行的比赛。")
            return

        lines = ["🔴 【HLTV 正在进行的比赛】", "------------------------------------"]
        for idx, m in enumerate(live_matches, 1):
            event_name = ((m.get("event") or {}).get("name") or "未知赛事").strip()
            team1 = (m.get("team1") or {}).get("name") or "待定"
            team2 = (m.get("team2") or {}).get("name") or "待定"
            fmt = self.scheduler.format_bo(m)
            m_id = m.get("id")
            star = "⭐ " if self.scheduler.is_tracked_match(m) else ""

            s1, s2 = self.scheduler.score_pair(m)
            score_str = ""
            if s1 is not None or s2 is not None:
                score_str = f" [当前大比分 {s1 or 0} - {s2 or 0}]"

            url = m.get("url") or f"https://www.hltv.org/matches/{m_id}"
            lines.append(f"{idx}. {star}🏆 {event_name} ({fmt})")
            lines.append(f"   ⚔️ {team1} 🆚 {team2}{score_str}")
            if m_id:
                lines.append(f"   🆔 比赛ID：{m_id}")
            lines.append(f"   🔗 {url}")
            lines.append("")

        if lines[-1] == "":
            lines.pop()
        lines.append("------------------------------------")
        lines.append("💡 发送 /hltv match <ID或战队> 查看全员KDA与Rating")
        yield event.plain_result("\n".join(lines))

    @hltv.command("results", alias={"战报", "最近战报", "最近赛果"})
    async def hltv_results(self, event: AstrMessageEvent):
        """查看关注赛事的近期完赛结果（按当地比赛日聚合）"""
        if not self.scheduler.get_tracked_event_ids():
            yield event.plain_result(
                "ℹ️ 尚未标记任何关注赛事。发送 /hltv events 查看赛事，再用 /hltv track <ID> 标记。"
            )
            return

        results = await self._collect_tracked_results(days=7)
        if not results:
            yield event.plain_result("🏁 关注赛事近 7 天暂无完赛记录。")
            return

        by_matchday: Dict[str, List[Dict[str, Any]]] = {}
        for m in results:
            ts = self.scheduler.parse_starts_at_ts(m)
            if ts:
                by_matchday.setdefault(self.scheduler.get_matchday(m, ts), []).append(m)

        current_matchday = self.scheduler.get_current_matchday()

        if by_matchday.get(current_matchday):
            today_matches = by_matchday[current_matchday]
            today_matches.sort(
                key=lambda x: self.scheduler.parse_starts_at_ts(x) or 0, reverse=True
            )
            msg = self.scheduler.format_matchday_results(
                today_matches, current_matchday, is_today=True
            )
        else:
            past_days = sorted(
                (d for d in by_matchday if d <= current_matchday), reverse=True
            )
            if not past_days:
                yield event.plain_result("🏁 关注赛事近期暂无完赛记录。")
                return

            target_day = past_days[0]
            day_matches = by_matchday[target_day]
            day_matches.sort(
                key=lambda x: self.scheduler.parse_starts_at_ts(x) or 0, reverse=True
            )

            hint: Optional[str] = None
            try:
                upcoming_by_day = await self._collect_tracked_matches(days=1)
                today_upcoming = [
                    m
                    for m in upcoming_by_day.get(current_matchday, [])
                    if m.get("status") in ("upcoming", "live")
                ]
                if today_upcoming:
                    first_m = today_upcoming[0]
                    first_ts = self.scheduler.parse_starts_at_ts(first_m) or 0
                    tz = self.scheduler.get_tz()
                    first_time_str = datetime.fromtimestamp(first_ts, tz=tz).strftime(
                        "%H:%M"
                    )
                    t1 = (first_m.get("team1") or {}).get("name") or "待定"
                    t2 = (first_m.get("team2") or {}).get("name") or "待定"
                    hint = f"今日关注赛事尚未完赛，首场对决（{t1} vs {t2}）将于 {first_time_str} 开打。"
            except Exception as e:
                logger.warning(f"[HLTV] 获取今日首场比赛提示失败: {e}", exc_info=True)

            msg = self.scheduler.format_matchday_results(
                day_matches, target_day, is_today=False, next_match_hint=hint
            )

        yield event.plain_result(msg)

    # ---------------- 订阅 ----------------

    @hltv.command("sub", alias={"订阅"})
    async def hltv_subscribe(self, event: AstrMessageEvent):
        """订阅本会话的 HLTV 比赛提醒与每日赛程"""
        origin = event.unified_msg_origin
        targets: List[str] = self.config.get("notify_targets", [])

        if origin in targets:
            yield event.plain_result("ℹ️ 当前会话已在订阅列表中，无需重复订阅。")
            return

        targets.append(origin)
        self.config["notify_targets"] = targets
        self._save_config()

        tracked_hint = ""
        if not self.scheduler.get_tracked_event_ids():
            tracked_hint = (
                "\n⚠️ 当前尚未标记任何关注赛事，管理员需先 /hltv track <赛事ID>。"
            )

        yield event.plain_result(
            "✅ 订阅成功！当前会话将接收关注赛事的：\n"
            "1. 开赛前 10 分钟提醒\n"
            "2. 赛后战报比分与选手数据推送\n"
            f"3. 每日固定时间 ({self.config.get('daily_schedule_time', '09:00')}) 赛程汇总\n"
            f"{tracked_hint}\n"
            "发送 /hltv unsub 可随时取消订阅。"
        )

    @hltv.command("unsub", alias={"取消订阅"})
    async def hltv_unsubscribe(self, event: AstrMessageEvent):
        """取消本会话的 HLTV 赛事推送订阅"""
        origin = event.unified_msg_origin
        targets: List[str] = self.config.get("notify_targets", [])

        if origin not in targets:
            yield event.plain_result("ℹ️ 当前会话未在订阅列表中。")
            return

        targets.remove(origin)
        self.config["notify_targets"] = targets
        self._save_config()

        yield event.plain_result("✅ 已成功取消当前会话的 HLTV 赛事提醒与赛程推送。")

    @hltv.command("status", alias={"状态"})
    async def hltv_status(self, event: AstrMessageEvent):
        """查看 HLTV 插件当前运行状态与配置"""
        origin = event.unified_msg_origin
        targets: List[str] = self.config.get("notify_targets", [])
        tracked = self.scheduler.get_tracked_event_ids()
        is_subbed = origin in targets

        api_base = self.config.get("api_base", "https://hltv.rinyin.top")
        server_ip = (self.config.get("server_ip") or "").strip()
        default_matchday_tz = self.config.get("matchday_timezone", "Europe/Berlin")
        daily_time = self.config.get("daily_schedule_time", "09:00")
        bo1_delay = self.config.get("bo1_delay_minutes", 45)
        bo3_delay = self.config.get("bo3_delay_minutes", 120)
        bo5_delay = self.config.get("bo5_delay_minutes", 240)
        retry_interval = self.config.get("result_retry_interval", 10)

        tracked_lines = (
            "\n".join(
                f"   - {eid} {self.scheduler.event_display_name(eid)}"
                for eid in tracked
            )
            if tracked
            else "   - （无，发送 /hltv track <ID> 标记）"
        )

        status_text = (
            "⚙️ 【HLTV 插件运行状态】\n"
            "------------------------------------\n"
            f"• 当前会话订阅：{'✅ 已订阅' if is_subbed else '❌ 未订阅'}\n"
            f"• 订阅会话总数：{len(targets)} 个\n"
            f"• 关注赛事（{len(tracked)} 个）：\n{tracked_lines}\n"
            f"• API 服务地址：{api_base} (直连: {server_ip})\n"
            f"• 比赛日对齐时区：按赛区动态识别 (兜底: {default_matchday_tz})\n"
            f"• 每日赛程推送时间：{daily_time}\n"
            "• 战报首次获取延迟：\n"
            f"   - BO1：开赛后 {bo1_delay} 分钟\n"
            f"   - BO3：开赛后 {bo3_delay} 分钟\n"
            f"   - BO5：开赛后 {bo5_delay} 分钟\n"
            f"• 战报未完赛重试间隔：每 {retry_interval} 分钟\n"
            f"• 当前追踪中比赛数：{len(self.scheduler.tracking_matches)} 场\n"
            f"• 历史已提醒比赛数：{len(self.scheduler.reminded_match_ids)} 场\n"
            "------------------------------------\n"
            "💡 /hltv sub 或 /hltv unsub 管理订阅；/hltv track 或 /hltv untrack 管理关注赛事"
        )
        yield event.plain_result(status_text)
