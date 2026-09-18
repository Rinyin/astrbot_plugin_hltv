import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

from .api import HLTVClient
from .scheduler import HLTVScheduler

logger = logging.getLogger("astrbot")


class Main(Star):
    """HLTV CS2 大型比赛自动提醒、战报获取与每日赛程推送插件"""

    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 确保 notify_targets 列表存在
        if "notify_targets" not in self.config:
            self.config["notify_targets"] = []

        api_base = self.config.get("api_base", "https://hltv.rinyin.top")
        server_ip = self.config.get("server_ip", "x.x.x.x")
        self.client = HLTVClient(base_url=api_base, server_ip=server_ip)
        self.scheduler = HLTVScheduler(
            context=self.context,
            config=self.config,
            client=self.client,
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
        """保存配置更改"""
        if isinstance(self.config, AstrBotConfig):
            try:
                self.config.save_config()
            except Exception as e:
                logger.error(f"[HLTV] 保存配置文件失败: {e}")

    # ==========================
    # 指令组: /hltv
    # ==========================

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
            "• /hltv today (或 /hltv 赛程)：查看今日大型赛事（当日打完自动切换明日预告）\n"
            "• /hltv results (或 /hltv 战报)：查看当天大型赛事完赛战报（按当地比赛日时区）\n"
            "• /hltv match <ID/战队> [图号]：查看比赛全员KDA/Rating及单图详细数据（支持绿龙、小蜜蜂等中文别名）\n"
            "• /hltv live (或 /hltv 正在进行)：查看当前正在进行的比赛及比分\n"
            "• /hltv refresh (或 /hltv 刷新)：强制刷新上游缓存，获取最新完赛比分与数据\n"
            "• /hltv sub (或 /hltv 订阅)：订阅当前聊天的赛前提醒与每日赛程\n"
            "• /hltv unsub (或 /hltv 取消订阅)：取消当前聊天订阅\n"
            "• /hltv status (或 /hltv 状态)：查看插件运行配置、比赛日时区与赛事级别说明\n"
            "------------------------------------\n"
            "💡 战队别名示例：支持绿龙(Spirit)、小蜜蜂(Vitality)、银河战舰/大表哥(FaZe)、老鼠(MOUZ)、A队(Astralis)、蒙古(The MongolZ)、天禄(TYLOO)等。"
        )
        yield event.plain_result(help_text)

    @hltv.command("refresh", alias={"刷新", "清除缓存"})
    async def hltv_refresh(self, event: AstrMessageEvent):
        """清除上游服务端缓存，强制获取最新实时赛程与比分"""
        ok = await self.client.clear_cache()
        if ok:
            yield event.plain_result("🔄 已成功向 HLTV 接口服务发送刷新指令并清除服务端缓存，已拉取最新实时数据！")
        else:
            yield event.plain_result("⚠️ 发送刷新指令失败，请稍后重试。")

    @hltv.command("match", alias={"比赛", "数据", "战报详情", "比赛详情"})
    async def hltv_match(self, event: AstrMessageEvent, query: str = "", map_arg: str = ""):
        """查看指定比赛的全员KDA、Rating及单图详细数据。用法: /hltv match <比赛ID或战队名称/别名> [图号]"""
        clean_q = query.strip()
        if not clean_q:
            yield event.plain_result(
                "ℹ️ 请提供要查询的比赛ID或战队名称（支持中文别名，如 绿龙、小蜜蜂、银河战舰、老鼠 等）。\n"
                "💡 格式：/hltv match <比赛ID或战队名> [图号]\n"
                "例如：\n"
                "  • /hltv match 2398026 （查看全场全员KDA、Rating）\n"
                "  • /hltv match 2398026 1 （查看图1单图选手数据）\n"
                "  • /hltv match 绿龙 （支持战队中文别名查询）\n"
                "  • /hltv match 2398026 -r （带 -r 强制刷新上游缓存）\n"
                "提示：发送 /hltv live、/hltv results 或 /hltv today 可获取比赛ID。"
            )
            return

        # 检测是否携带强制刷新标记（如 /hltv match 2398026 -r 或 /hltv match 绿龙 刷新）
        force_refresh = False
        for flag in ("-r", "--refresh", "刷新", "-f"):
            if clean_q.endswith(f" {flag}"):
                force_refresh = True
                clean_q = clean_q[:-len(flag)].strip()
                break
            elif clean_q == flag:
                force_refresh = True
                clean_q = ""
                break

        if not clean_q:
            clean_q = map_arg.strip()
            map_arg = ""

        detail = await self.client.find_match(clean_q, force_refresh=force_refresh)
        if not detail:
            yield event.plain_result(f"❌ 未找到与「{clean_q}」相关的比赛数据。\n提示：可使用数字比赛ID或战队名（如 绿龙/Spirit）重新查询。")
            return

        # 若 find_match 仅返回简略信息，且有 id，尝试获取全量 detail
        if ("player_stats" not in detail or not detail.get("player_stats")) and detail.get("id"):
            full_detail = await self.client.get_match_detail(str(detail.get("id")))
            if full_detail:
                detail = full_detail

        msg = self.scheduler.format_match_detail(detail, map_query=map_arg.strip() if map_arg else None)
        yield event.plain_result(msg)

    @hltv.command("today", alias={"赛程", "今日赛程"})
    async def hltv_today(self, event: AstrMessageEvent):
        """查看今日焦点赛事（按比赛日时区）。若今日比赛均已完赛，自动顺延展示明日预告。"""
        # 获取未来几天的比赛并过滤大型/精英赛事
        all_matches = await self.client.get_matches(status="all", days=5, limit=150)
        major_matches = []
        for m in all_matches:
            if await self.scheduler.is_major_match(m):
                major_matches.append(m)

        current_matchday = self.scheduler.get_current_matchday()

        # 按比赛日时区归类
        by_matchday: Dict[str, List[Dict[str, Any]]] = {}
        for m in major_matches:
            ts = self.scheduler._parse_starts_at_ts(m)
            if ts:
                m_day = self.scheduler.get_matchday(m, ts)
                by_matchday.setdefault(m_day, []).append(m)

        today_matches = by_matchday.get(current_matchday, [])
        today_matches.sort(key=lambda x: self.scheduler._parse_starts_at_ts(x) or 0)

        # 判断今日是否还有正在进行或尚未开赛的比赛
        has_active_matches = False
        for m in today_matches:
            st = m.get("status")
            if st in ("upcoming", "live"):
                has_active_matches = True
                break

        if today_matches and has_active_matches:
            # 今日仍有比赛未打完，展示今日完整焦点赛程
            msg = self.scheduler.format_daily_schedule(today_matches, current_matchday, is_next_day=False)
        else:
            # 今日焦点赛事已全部打完或今日无大型比赛，自动展示次日赛程预告
            future_days = sorted([d for d in by_matchday.keys() if d > current_matchday])
            if future_days:
                next_day = future_days[0]
                next_matches = by_matchday[next_day]
                next_matches.sort(key=lambda x: self.scheduler._parse_starts_at_ts(x) or 0)
                msg = self.scheduler.format_daily_schedule(next_matches, next_day, is_next_day=True)
            elif today_matches:
                msg = self.scheduler.format_daily_schedule(today_matches, current_matchday, is_next_day=False)
            else:
                msg = f"📅 【HLTV 赛程预告】\n比赛日 {current_matchday} 及近期暂无大型焦点赛事安排。"

        yield event.plain_result(msg)

    @hltv.command("live", alias={"正在进行", "实时"})
    async def hltv_live(self, event: AstrMessageEvent):
        """查看当前正在进行的比赛"""
        live_matches = await self.client.get_live_matches()

        if not live_matches:
            yield event.plain_result("🔴 当前暂无正在进行的比赛。")
            return

        lines = [
            "🔴 【HLTV 正在进行的比赛】",
            "------------------------------------",
        ]

        for idx, m in enumerate(live_matches, 1):
            event_name = (m.get("event") or {}).get("name") or "未知赛事"
            team1 = (m.get("team1") or {}).get("name") or "待定"
            team2 = (m.get("team2") or {}).get("name") or "待定"
            fmt = (m.get("format") or "BO3").upper()
            tier = await self.scheduler.get_match_tier(m)
            m_id = m.get("id")

            score = m.get("score")
            score_str = ""
            if score and isinstance(score, dict):
                score_str = f" [当前大比分 {score.get('team1', 0)} - {score.get('team2', 0)}]"

            url = m.get("url") or f"https://www.hltv.org/matches/{m_id}"

            lines.append(f"{idx}. 🏆 {event_name} | [{tier}] ({fmt})")
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
        """查看近期焦点完赛结果（按比赛日时区聚合，仅显示大型/精英赛事）"""
        results = await self.client.get_results(days=7, limit=100)

        if not results:
            yield event.plain_result("🏁 近期暂无完赛记录。")
            return

        # 过滤大型/精英赛事
        major_results: List[Dict[str, Any]] = []
        for m in results:
            if await self.scheduler.is_major_match(m):
                major_results.append(m)

        if not major_results:
            yield event.plain_result("🏁 近期暂无符合级别要求的大型/精英赛事完赛记录。")
            return

        # 按比赛日时区聚合
        by_matchday: Dict[str, List[Dict[str, Any]]] = {}
        for m in major_results:
            ts = self.scheduler._parse_starts_at_ts(m)
            if ts:
                m_day = self.scheduler.get_matchday(m, ts)
                by_matchday.setdefault(m_day, []).append(m)

        current_matchday = self.scheduler.get_current_matchday()

        # 检查今日比赛日是否有已完赛比赛
        if current_matchday in by_matchday and by_matchday[current_matchday]:
            today_matches = by_matchday[current_matchday]
            today_matches.sort(key=lambda x: self.scheduler._parse_starts_at_ts(x) or 0, reverse=True)
            msg = self.scheduler.format_matchday_results(today_matches, current_matchday, is_today=True)
        else:
            # 今日尚未有完赛比赛，展示最近的一个已完赛比赛日全部赛果
            past_days = sorted([d for d in by_matchday.keys() if d <= current_matchday], reverse=True)
            if past_days:
                target_day = past_days[0]
                day_matches = by_matchday[target_day]
                day_matches.sort(key=lambda x: self.scheduler._parse_starts_at_ts(x) or 0, reverse=True)

                # 提示今日首场开赛信息
                hint = None
                try:
                    upcoming = await self.client.get_upcoming_matches(days=1, limit=50)
                    today_upcoming = []
                    tz = self.scheduler._get_tz()
                    for m in upcoming:
                        if await self.scheduler.is_major_match(m):
                            ts = self.scheduler._parse_starts_at_ts(m)
                            if ts and self.scheduler.get_matchday(m, ts) == current_matchday:
                                today_upcoming.append((ts, m))
                    if today_upcoming:
                        today_upcoming.sort(key=lambda x: x[0])
                        first_ts, first_m = today_upcoming[0]
                        first_time_str = datetime.fromtimestamp(first_ts, tz=tz).strftime("%H:%M")
                        t1 = (first_m.get("team1") or {}).get("name") or "待定"
                        t2 = (first_m.get("team2") or {}).get("name") or "待定"
                        hint = f"今日焦点战尚未完赛，首场对决（{t1} vs {t2}）将于 {first_time_str} 开打。"
                except Exception:
                    pass

                msg = self.scheduler.format_matchday_results(
                    day_matches, target_day, is_today=False, next_match_hint=hint
                )
            else:
                msg = "🏁 近期暂无大型/精英赛事完赛记录。"

        yield event.plain_result(msg)

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

        yield event.plain_result(
            "✅ 订阅成功！当前会话将接收：\n"
            "1. 焦点比赛开赛前 10 分钟提醒\n"
            "2. 赛后战报比分与选手数据推送\n"
            f"3. 每日固定时间 ({self.config.get('daily_schedule_time', '09:00')}) 赛程汇总\n\n"
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
        is_subbed = origin in targets

        api_base = self.config.get("api_base", "https://hltv.rinyin.top")
        server_ip = self.config.get("server_ip", "x.x.x.x")
        default_matchday_tz = self.config.get("default_matchday_timezone", self.config.get("matchday_timezone", "Europe/Berlin"))
        tracked_tiers = self.config.get("tracked_tiers", ["T1", "T2"])
        daily_time = self.config.get("daily_schedule_time", "09:00")
        bo1_delay = self.config.get("bo1_delay_minutes", 45)
        bo3_delay = self.config.get("bo3_delay_minutes", 120)
        bo5_delay = self.config.get("bo5_delay_minutes", 240)
        retry_interval = self.config.get("result_retry_interval", 10)

        status_text = (
            "⚙️ 【HLTV 插件运行状态】\n"
            "------------------------------------\n"
            f"• 当前会话订阅：{'✅ 已订阅' if is_subbed else '❌ 未订阅'}\n"
            f"• 订阅会话总数：{len(targets)} 个\n"
            f"• API 服务地址：{api_base} (直连: {server_ip})\n"
            f"• 赛事追踪级别：{'/'.join(tracked_tiers)} 级别赛事\n"
            f"• 比赛日对齐时区：全球多赛区动态识别 (兜底: {default_matchday_tz})\n"
            f"• 每日赛程推送时间：{daily_time}\n"
            f"• 战报首次获取延迟：\n"
            f"   - BO1：开赛后 {bo1_delay} 分钟\n"
            f"   - BO3：开赛后 {bo3_delay} 分钟\n"
            f"   - BO5：开赛后 {bo5_delay} 分钟\n"
            f"• 战报未完赛重试间隔：每 {retry_interval} 分钟\n"
            f"• 当前追踪中比赛数：{len(self.scheduler.tracking_matches)} 场\n"
            f"• 历史已提醒比赛数：{len(self.scheduler.reminded_match_ids)} 场\n"
            "------------------------------------\n"
            "💡 使用 /hltv sub 或 /hltv unsub 管理当前聊天订阅"
        )
        yield event.plain_result(status_text)
