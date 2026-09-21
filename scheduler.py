import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, StarTools

from .api import HLTVClient

# 全球常见赛区与城市时区映射表（用于按比赛当地真实时区动态划分比赛日）
REGION_TIMEZONES: Dict[str, str] = {
    "europe": "Europe/Berlin",
    "cis": "Europe/Moscow",
    "americas": "America/New_York",
    "north america": "America/New_York",
    "south america": "America/Sao_Paulo",
    "brazil": "America/Sao_Paulo",
    "asia": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "oceania": "Australia/Sydney",
    "australia": "Australia/Sydney",
}

CITY_KEYWORD_TIMEZONES: List[Tuple[List[str], str]] = [
    # 南美赛区
    (
        [
            "curitiba",
            "rio",
            "sao paulo",
            "brasil",
            "brazil",
            "argentina",
            "chile",
            "cbcs",
            "south america",
            "sa ",
        ],
        "America/Sao_Paulo",
    ),
    # 北美赛区
    (
        [
            "dallas",
            "atlanta",
            "austin",
            "chicago",
            "los angeles",
            "columbus",
            "north america",
            "na ",
        ],
        "America/New_York",
    ),
    # 亚洲与中国赛区
    (
        [
            "shanghai",
            "chengdu",
            "beijing",
            "china",
            "mongolia",
            "ulaanbaatar",
            "singapore",
            "asia",
        ],
        "Asia/Shanghai",
    ),
    # 大洋洲赛区
    (
        ["melbourne", "sydney", "australia", "oceania", "nz", "new zealand"],
        "Australia/Sydney",
    ),
    # 独联体赛区
    (["moscow", "almaty", "cis"], "Europe/Moscow"),
    # 欧洲赛区 (世界 CS2 核心赛区)
    (
        [
            "cologne",
            "katowice",
            "copenhagen",
            "paris",
            "london",
            "malta",
            "warsaw",
            "espoo",
            "valencia",
            "berlin",
            "bucharest",
            "belgrade",
            "epl",
            "starladder",
            "blast",
            "iem",
            "europe",
            "european",
            "nordic",
            "sweden",
            "denmark",
            "germany",
        ],
        "Europe/Berlin",
    ),
]


class HLTVScheduler:
    """HLTV 比赛提醒、战报追踪与每日赛程调度器"""

    def __init__(
        self,
        context: Context,
        config: dict,
        client: HLTVClient,
        plugin_name: str = "astrbot_plugin_hltv",
    ):
        self.context = context
        self.config = config
        self.client = client

        self._task: Optional[asyncio.Task] = None
        self._running = False

        # 状态追踪
        self.reminded_match_ids: Set[str] = set()
        self.tracking_matches: Dict[str, Dict[str, Any]] = {}
        self.reported_match_ids: Set[str] = set()
        self.last_daily_date: Optional[str] = None

        # 已标记赛事的名称缓存 {event_id: name}，用于在消息中显示赛事名
        self.event_names: Dict[str, str] = {}

        # 运行状态持久化文件，存放于 data/plugin_data/<plugin_name>/ 下（框架标准数据目录）
        data_dir = StarTools.get_data_dir(plugin_name)
        self.state_file_path = os.path.join(str(data_dir), "state.json")

        self._load_state()

    def get_tz(self) -> ZoneInfo:
        """用户显示时区（默认 Asia/Shanghai，即北京时间）"""
        tz_name = self.config.get("timezone", "Asia/Shanghai")
        try:
            return ZoneInfo(tz_name)
        except Exception as e:
            logger.warning(f"[HLTV] 显示时区 {tz_name} 无效，退回 Asia/Shanghai: {e}")
            return ZoneInfo("Asia/Shanghai")

    def get_default_matchday_tz(self) -> ZoneInfo:
        """兜底比赛日时区（当比赛无法推断赛区时使用，默认 Europe/Berlin 欧洲当地时区）"""
        tz_name = self.config.get("matchday_timezone", "Europe/Berlin")
        try:
            return ZoneInfo(tz_name)
        except Exception as e:
            logger.warning(
                f"[HLTV] 兜底比赛日时区 {tz_name} 无效，退回 Europe/Berlin: {e}"
            )
            return ZoneInfo("Europe/Berlin")

    def get_match_timezone(self, match: Optional[Dict[str, Any]]) -> ZoneInfo:
        """根据比赛的 region 字段或赛事名称/城市关键词动态识别比赛当地时区。
        若比赛无地区信息或无法识别，回退至兜底比赛日时区。
        """
        if not match:
            return self.get_default_matchday_tz()

        # 1. 优先读取比赛对象的 region 字段
        region = (match.get("region") or "").strip().lower()
        if region in REGION_TIMEZONES:
            tz = self._safe_zoneinfo(REGION_TIMEZONES[region])
            if tz:
                return tz

        # 2. 匹配赛事名称/城市关键词
        ev_name = ((match.get("event") or {}).get("name") or "").lower()
        for kw, tz_name in REGION_TIMEZONES.items():
            if kw in ev_name:
                tz = self._safe_zoneinfo(tz_name)
                if tz:
                    return tz

        for kws, tz_name in CITY_KEYWORD_TIMEZONES:
            if any(kw in ev_name for kw in kws):
                tz = self._safe_zoneinfo(tz_name)
                if tz:
                    return tz

        # 3. 兜底
        return self.get_default_matchday_tz()

    @staticmethod
    def _safe_zoneinfo(tz_name: str) -> Optional[ZoneInfo]:
        """安全构造 ZoneInfo，时区名无效时返回 None 并记录日志"""
        try:
            return ZoneInfo(tz_name)
        except Exception as e:
            logger.warning(f"[HLTV] 时区映射表中的时区名 {tz_name} 无效: {e}")
            return None

    def get_matchday(self, match_or_ts: Any, ts: Optional[float] = None) -> str:
        """获取比赛对应的当地比赛日字符串（格式 YYYY-MM-DD）。
        支持传入 match 字典或单独的时间戳。传入 match 字典时按该比赛真实当地时区动态换算。
        """
        match = None
        target_ts = None
        if isinstance(match_or_ts, dict):
            match = match_or_ts
            target_ts = ts if ts is not None else self.parse_starts_at_ts(match)
        elif isinstance(match_or_ts, (int, float)):
            target_ts = float(match_or_ts)

        if target_ts is None:
            return self.get_current_matchday()

        tz = self.get_match_timezone(match)
        dt = datetime.fromtimestamp(target_ts, tz=tz)
        return dt.strftime("%Y-%m-%d")

    def get_current_matchday(self) -> str:
        """获取当前用户参考比赛日字符串（以用户显示时区当前日期为基准）"""
        now = datetime.now(tz=self.get_tz())
        return now.strftime("%Y-%m-%d")

    def get_next_matchday(self, days: int = 1) -> str:
        """获取后续比赛日字符串（以用户显示时区当前日期为基准）"""
        now = datetime.now(tz=self.get_tz())
        return (now + timedelta(days=days)).strftime("%Y-%m-%d")

    def _load_state(self) -> None:
        """从持久化文件读取状态"""
        if not os.path.exists(self.state_file_path):
            return
        try:
            with open(self.state_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.reminded_match_ids = set(data.get("reminded_match_ids", []))
                self.tracking_matches = data.get("tracking_matches", {})
                self.reported_match_ids = set(data.get("reported_match_ids", []))
                self.last_daily_date = data.get("last_daily_date")
                self.event_names = dict(data.get("event_names", {}))
                logger.info(
                    f"[HLTV] 载入运行状态: 已提醒 {len(self.reminded_match_ids)} 场, "
                    f"正在追踪 {len(self.tracking_matches)} 场, 已播报战报 {len(self.reported_match_ids)} 场"
                )
        except Exception as e:
            logger.error(f"[HLTV] 载入状态文件失败: {e}", exc_info=True)

    def _save_state(self) -> None:
        """持久化保存状态"""
        try:
            # 限制历史集合大小，防止无限增大
            reminded_list = list(self.reminded_match_ids)[-1000:]
            reported_list = list(self.reported_match_ids)[-1000:]

            data = {
                "reminded_match_ids": reminded_list,
                "tracking_matches": self.tracking_matches,
                "reported_match_ids": reported_list,
                "last_daily_date": self.last_daily_date,
                "event_names": self.event_names,
            }
            with open(self.state_file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[HLTV] 保存状态文件失败: {e}", exc_info=True)

    def start(self) -> None:
        """启动后台轮询任务"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[HLTV] 后台调度轮询任务已启动。")

    async def stop(self) -> None:
        """停止后台轮询任务"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._save_state()
        logger.info("[HLTV] 后台调度轮询任务已停止。")

    async def broadcast_message(self, text: str) -> None:
        """向所有已订阅的目标广播消息"""
        targets: List[str] = self.config.get("notify_targets", [])
        if not targets:
            logger.debug("[HLTV] 未配置 notify_targets，跳过广播消息。")
            return

        for target in targets:
            try:
                await self.context.send_message(target, MessageChain().message(text))
                await asyncio.sleep(0.3)  # 限速保护
            except Exception as e:
                logger.error(f"[HLTV] 向目标 {target} 发送消息失败: {e}", exc_info=True)

    def parse_starts_at_ts(self, match: Dict[str, Any]) -> Optional[float]:
        """解析比赛开始时间戳（秒）"""
        unix_ms = match.get("starts_at_unix_ms")
        if unix_ms:
            return unix_ms / 1000.0

        starts_at_str = match.get("starts_at")
        if starts_at_str:
            try:
                dt = datetime.fromisoformat(starts_at_str.replace("Z", "+00:00"))
                return dt.timestamp()
            except (ValueError, TypeError) as e:
                logger.debug(f"[HLTV] 无法解析比赛开始时间 {starts_at_str!r}: {e}")
        return None

    # ------------------------------------------------------------------
    # 手动标记赛事
    # ------------------------------------------------------------------

    def get_tracked_event_ids(self) -> List[str]:
        """读取配置中手动标记的赛事 ID 列表（去重、保持顺序）"""
        raw = self.config.get("tracked_events") or []
        ids: List[str] = []
        for item in raw:
            item_str = str(item).strip()
            if item_str and item_str not in ids:
                ids.append(item_str)
        return ids

    def is_tracked_match(self, match: Dict[str, Any]) -> bool:
        """比赛是否属于已标记赛事（按赛事 ID 判断）"""
        event_id = str((match.get("event") or {}).get("id") or "").strip()
        return bool(event_id) and event_id in self.get_tracked_event_ids()

    def remember_event_name(self, event_id: str, name: Optional[str]) -> None:
        """缓存赛事名称，供状态与推送消息展示"""
        if name and self.event_names.get(event_id) != name:
            self.event_names[event_id] = name
            self._save_state()

    def event_display_name(self, event_id: str) -> str:
        return self.event_names.get(event_id) or f"赛事 {event_id}"

    @staticmethod
    def format_bo(match: Dict[str, Any]) -> str:
        """赛制展示：bo3 -> BO3；弃权/未知时给出可读文案而非错误值"""
        if match.get("forfeit"):
            return "弃权"
        fmt = (match.get("format") or "").strip()
        if re.fullmatch(r"bo\d+", fmt, re.I):
            return fmt.upper()
        return "BO?"

    @staticmethod
    def score_pair(match: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        score = match.get("score") or {}
        return score.get("team1"), score.get("team2")

    def get_format_delay_minutes(self, fmt: Optional[str]) -> int:
        """根据赛制获取延迟获取战报的分钟数"""
        fmt_lower = (fmt or "").lower()
        if "bo5" in fmt_lower:
            return int(self.config.get("bo5_delay_minutes", 240))
        elif "bo3" in fmt_lower:
            return int(self.config.get("bo3_delay_minutes", 120))
        else:
            return int(self.config.get("bo1_delay_minutes", 45))

    def format_match_reminder(self, match: Dict[str, Any]) -> str:
        """格式化开赛前 10 分钟提醒消息"""
        event_info = match.get("event") or {}
        event_name = (event_info.get("name") or "未知赛事").strip()
        stage = match.get("stage")
        stage_str = f" ({stage})" if stage else ""

        team1 = match.get("team1") or {}
        team2 = match.get("team2") or {}
        team1_name = team1.get("name") or "待定"
        team2_name = team2.get("name") or "待定"

        fmt = self.format_bo(match)

        tz = self.get_tz()
        starts_ts = self.parse_starts_at_ts(match)
        time_str = "即将开赛"
        if starts_ts:
            dt = datetime.fromtimestamp(starts_ts, tz=tz)
            time_str = dt.strftime("%H:%M")

        match_id = match.get("id") or ""
        url = match.get("url") or (
            f"https://www.hltv.org/matches/{match_id}" if match_id else ""
        )

        id_str = f"🆔 比赛ID：{match_id}\n" if match_id else ""
        return (
            f"📢 【HLTV 关注赛事即将开始】\n"
            f"🏆 赛事：{event_name}{stage_str}\n"
            f"⚔️ 对阵：{team1_name} 🆚 {team2_name} ({fmt})\n"
            f"⏰ 开赛时间：{time_str} (约 10 分钟后开打)\n"
            f"{id_str}"
            f"🔗 比赛详情：{url}"
        )

    @staticmethod
    def format_player_row(p: Dict[str, Any]) -> str:
        """格式化单名选手的完整战绩行：昵称、K-D (+/-)、ADR、Rating、KAST"""
        nick = p.get("nickname") or "未知"
        kd = p.get("kills_deaths") or "-"
        kd_diff = p.get("kd_diff")
        diff_str = f" ({kd_diff:+d})" if kd_diff is not None else ""
        adr = p.get("adr")
        adr_str = f" | ADR {adr:.1f}" if adr is not None else ""
        rating = p.get("rating")
        rating_str = f" | Rating {rating:.2f}" if rating is not None else ""
        kast = p.get("kast")
        kast_str = f" | KAST {kast}" if kast else ""
        return f"  • {nick}: {kd}{diff_str}{adr_str}{rating_str}{kast_str}"

    @staticmethod
    def group_players_by_team(
        player_stats: List[Dict[str, Any]], team1_name: str, team2_name: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """将选手统计数据按两支战队归类，并按 Rating 从高到低排序"""
        team1_players: List[Dict[str, Any]] = []
        team2_players: List[Dict[str, Any]] = []
        other_players: List[Dict[str, Any]] = []

        t1_low = (team1_name or "").strip().lower()
        t2_low = (team2_name or "").strip().lower()

        for p in player_stats:
            p_team = (p.get("team") or "").strip().lower()
            if p_team and t1_low and (p_team in t1_low or t1_low in p_team):
                team1_players.append(p)
            elif p_team and t2_low and (p_team in t2_low or t2_low in p_team):
                team2_players.append(p)
            else:
                other_players.append(p)

        if not team1_players and not team2_players and player_stats:
            mid = len(player_stats) // 2
            team1_players = player_stats[:mid]
            team2_players = player_stats[mid:]
        elif other_players:
            for p in other_players:
                if len(team1_players) < 5:
                    team1_players.append(p)
                else:
                    team2_players.append(p)

        team1_players.sort(key=lambda x: float(x.get("rating") or 0.0), reverse=True)
        team2_players.sort(key=lambda x: float(x.get("rating") or 0.0), reverse=True)
        return team1_players, team2_players

    @staticmethod
    def format_map_lines(
        detail: Dict[str, Any], team1_name: str, team2_name: str
    ) -> List[str]:
        """逐图比分行；跳过尚未开打（比分为空）或 TBA 占位的地图"""
        lines: List[str] = []
        if detail.get("forfeit"):
            return lines
        for idx, m in enumerate(detail.get("maps") or [], 1):
            map_name = m.get("name") or f"Map {idx}"
            if map_name.upper() == "TBA":
                continue
            m_s1, m_s2 = m.get("team1_score"), m.get("team2_score")
            if m_s1 is None and m_s2 is None:
                continue
            picked_by = m.get("picked_by")
            pick_str = f" ({picked_by} 选图)" if picked_by else ""
            half = m.get("half_scores") or []
            half_str = f" [半场 {', '.join(half)}]" if half else ""
            lines.append(
                f"  • 图{idx} {map_name}：{team1_name} {m_s1 if m_s1 is not None else '-'} - "
                f"{m_s2 if m_s2 is not None else '-'} {team2_name}{pick_str}{half_str}"
            )
        return lines

    def format_match_result(self, detail: Dict[str, Any]) -> str:
        """格式化赛后完赛战报消息（包含全员KDA、ADR与Rating）"""
        match_id = str(detail.get("id") or "")
        event_info = detail.get("event") or {}
        event_name = (event_info.get("name") or "HLTV 赛事").strip()

        team1 = detail.get("team1") or {}
        team2 = detail.get("team2") or {}
        team1_name = team1.get("name") or "队伍1"
        team2_name = team2.get("name") or "队伍2"

        s1, s2 = self.score_pair(detail)

        winner_name = None
        if s1 is not None and s2 is not None:
            if s1 > s2:
                winner_name = team1_name
            elif s2 > s1:
                winner_name = team2_name

        winner_str = f" 🏆 {winner_name} 获胜！" if winner_name else ""
        fmt = self.format_bo(detail)
        score_str = f"{s1 if s1 is not None else '-'} - {s2 if s2 is not None else '-'}"

        lines = [
            "🏁 【HLTV 比赛战报】",
            f"🏆 赛事：{event_name}",
            f"⚔️ 总比分：{team1_name} {score_str} {team2_name} ({fmt}){winner_str}",
            "------------------------",
        ]
        if detail.get("forfeit"):
            lines.append("ℹ️ 本场比赛以弃权/判负结束，无地图与选手数据。")
            lines.append("------------------------")

        # 地图详情（仅列出已打完/正在进行的地图）
        map_lines = self.format_map_lines(detail, team1_name, team2_name)
        if map_lines:
            lines.append("🗺️ 地图详情：")
            lines.extend(map_lines)
            lines.append("------------------------")

        # 选手表现 (全员 KDA, ADR, Rating, KAST，分队显示)
        player_stats = detail.get("player_stats", [])
        if player_stats:
            t1_players, t2_players = self.group_players_by_team(
                player_stats, team1_name, team2_name
            )
            if t1_players:
                lines.append(f"🔹 {team1_name} 全场选手数据：")
                for p in t1_players:
                    lines.append(self.format_player_row(p))
            if t2_players:
                lines.append(f"🔸 {team2_name} 全场选手数据：")
                for p in t2_players:
                    lines.append(self.format_player_row(p))
            lines.append("------------------------")

        if match_id:
            lines.append(f"🆔 比赛ID：{match_id}")
            lines.append(f"💡 发送 /hltv match {match_id} <图号> 可查看单图全员数据")
        url = detail.get("url") or (
            f"https://www.hltv.org/matches/{match_id}" if match_id else ""
        )
        if url:
            lines.append(f"🔗 HLTV页面：{url}")

        return "\n".join(lines)

    def format_match_detail(
        self, detail: Dict[str, Any], map_query: Optional[str] = None
    ) -> str:
        """格式化比赛详细数据，支持全场统计或单图数据查询"""
        match_id = str(detail.get("id") or "")
        event_info = detail.get("event") or {}
        event_name = (event_info.get("name") or "HLTV 赛事").strip()

        team1 = detail.get("team1") or {}
        team2 = detail.get("team2") or {}
        team1_name = team1.get("name") or "队伍1"
        team2_name = team2.get("name") or "队伍2"

        s1, s2 = self.score_pair(detail)

        fmt = self.format_bo(detail)
        status = detail.get("status") or "未知"
        status_map = {
            "finished": "已完赛",
            "live": "正在进行",
            "upcoming": "未开赛",
        }
        status_str = status_map.get(status, status)

        maps = detail.get("maps", [])
        map_player_stats: Dict[str, List[Dict[str, Any]]] = (
            detail.get("map_player_stats") or {}
        )

        # 如果指定了地图编号或名称
        if map_query:
            target_map_name = None
            target_map_idx = None
            target_map_obj = None

            clean_q = map_query.strip().lower()

            # 1. 尝试按纯数字图号匹配 (例如 "1", "2", "3")
            if clean_q.isdigit():
                idx_val = int(clean_q)
                if 1 <= idx_val <= len(maps):
                    target_map_idx = idx_val
                    target_map_obj = maps[idx_val - 1]
                    target_map_name = target_map_obj.get("name")
            else:
                # 2. 尝试按地图名称匹配 (如 "inferno", "de_inferno", "anubis" 等)
                for idx, m in enumerate(maps, 1):
                    m_name = (m.get("name") or "").lower()
                    if (
                        clean_q in m_name
                        or m_name in clean_q
                        or clean_q.replace("de_", "") in m_name
                    ):
                        target_map_idx = idx
                        target_map_obj = m
                        target_map_name = m.get("name")
                        break

                # 3. 若 maps 中没匹配到，在 map_player_stats key 中匹配
                if not target_map_name:
                    for k in map_player_stats.keys():
                        if clean_q in k.lower() or k.lower() in clean_q:
                            target_map_name = k
                            break

            if not target_map_name:
                available_maps = [
                    f"图{i} {m.get('name')}" for i, m in enumerate(maps, 1)
                ]
                avail_str = "、".join(available_maps) if available_maps else "暂无"
                return (
                    f"⚠️ 未找到对应地图「{map_query}」。\n"
                    f"当前比赛可用地图：{avail_str}\n"
                    f"💡 示例用法：/hltv match {match_id} 1"
                )

            # 寻找该地图的选手数据
            p_stats = map_player_stats.get(target_map_name, [])
            if not p_stats:
                for k, v in map_player_stats.items():
                    if k.lower() == target_map_name.lower():
                        p_stats = v
                        break

            m_title = (
                f"图{target_map_idx} {target_map_name}"
                if target_map_idx
                else target_map_name
            )
            lines = [
                f"📊 【HLTV 单图数据详情 - {m_title}】",
                f"🏆 赛事：{event_name}",
            ]

            if target_map_obj:
                m_s1 = target_map_obj.get("team1_score")
                m_s2 = target_map_obj.get("team2_score")
                m_s1 = m_s1 if m_s1 is not None else "-"
                m_s2 = m_s2 if m_s2 is not None else "-"
                picked_by = target_map_obj.get("picked_by")
                pick_str = f" ({picked_by} 选图)" if picked_by else ""
                half_scores = target_map_obj.get("half_scores") or []
                half_str = f" (半场: {', '.join(half_scores)})" if half_scores else ""
                lines.append(
                    f"⚔️ 比分：{team1_name} {m_s1} - {m_s2} {team2_name}{pick_str}{half_str}"
                )
            lines.append("------------------------")

            if p_stats:
                t1_p, t2_p = self.group_players_by_team(p_stats, team1_name, team2_name)
                if t1_p:
                    lines.append(f"🔹 {team1_name} 选手数据：")
                    for p in t1_p:
                        lines.append(self.format_player_row(p))
                if t2_p:
                    lines.append(f"🔸 {team2_name} 选手数据：")
                    for p in t2_p:
                        lines.append(self.format_player_row(p))
            else:
                lines.append("⚠️ 暂无该单图的详细选手表现数据（可能正在进行或尚未录入）")

            lines.append("------------------------")
            if match_id:
                lines.append(f"🆔 比赛ID：{match_id}")
                lines.append(f"💡 发送 /hltv match {match_id} 可返回查看全场总数据")
            url = detail.get("url") or (
                f"https://www.hltv.org/matches/{match_id}" if match_id else ""
            )
            if url:
                lines.append(f"🔗 HLTV页面：{url}")
            return "\n".join(lines)

        # 否则显示全场比赛总览与全员数据
        winner_name = None
        if s1 is not None and s2 is not None and status == "finished":
            if s1 > s2:
                winner_name = team1_name
            elif s2 > s1:
                winner_name = team2_name
        winner_str = f" 🏆 {winner_name} 获胜！" if winner_name else ""
        score_str = f"{s1 if s1 is not None else '-'} - {s2 if s2 is not None else '-'}"

        lines = [
            "📊 【HLTV 比赛数据详情 - 全场统计】",
            f"🏆 赛事：{event_name}",
            f"⚔️ 总比分：{team1_name} {score_str} {team2_name} ({fmt}) [{status_str}]{winner_str}",
            "------------------------",
        ]
        if detail.get("forfeit"):
            lines.append("ℹ️ 本场比赛以弃权/判负结束，无地图与选手数据。")
            lines.append("------------------------")

        # 地图比分概览
        map_lines = self.format_map_lines(detail, team1_name, team2_name)
        if map_lines:
            lines.append("🗺️ 各图战况：")
            lines.extend(map_lines)
            lines.append("------------------------")

        # 全场选手数据 (全员 KDA, ADR, Rating, KAST)
        player_stats = detail.get("player_stats", [])
        if player_stats:
            t1_players, t2_players = self.group_players_by_team(
                player_stats, team1_name, team2_name
            )
            if t1_players:
                lines.append(f"🔹 {team1_name} 全场选手数据：")
                for p in t1_players:
                    lines.append(self.format_player_row(p))
            if t2_players:
                lines.append(f"🔸 {team2_name} 全场选手数据：")
                for p in t2_players:
                    lines.append(self.format_player_row(p))
            lines.append("------------------------")
        elif not detail.get("forfeit"):
            if status == "upcoming":
                lines.append("ℹ️ 比赛尚未开始，开赛后可查看选手数据。")
            elif status == "live":
                lines.append("ℹ️ 比赛进行中，选手数据将在首图结束后逐步更新。")
            else:
                lines.append("⚠️ 暂无选手数据（HLTV 尚未录入）")
            lines.append("------------------------")

        if match_id:
            lines.append(f"🆔 比赛ID：{match_id}")
            if maps:
                lines.append(
                    f"💡 发送 /hltv match {match_id} <图号> 查看单图数据（例如: /hltv match {match_id} 1）"
                )
        url = detail.get("url") or (
            f"https://www.hltv.org/matches/{match_id}" if match_id else ""
        )
        if url:
            lines.append(f"🔗 HLTV页面：{url}")

        return "\n".join(lines)

    def format_daily_schedule(
        self,
        matches: List[Dict[str, Any]],
        matchday_str: str,
        is_next_day: bool = False,
    ) -> str:
        """格式化比赛日已标记赛事的赛程

        Args:
            matches: 已标记赛事的比赛列表
            matchday_str: 比赛日字符串 (YYYY-MM-DD，按 matchday_timezone 划分)
            is_next_day: 是否为当日全部完赛后自动展示的次日预告
        """
        tz = self.get_tz()
        now = datetime.now(tz=tz)
        today_local_date = now.strftime("%Y-%m-%d")

        if is_next_day:
            header_title = (
                f"📅 【HLTV 今日关注赛程已全部结束，明日预告 (比赛日 {matchday_str})】"
            )
        else:
            header_title = f"📅 【HLTV 今日关注赛事预告 (比赛日 {matchday_str})】"

        lines = [
            header_title,
            "------------------------------------",
        ]

        if not matches:
            lines.append("当前比赛日已标记的赛事没有比赛安排。")
        else:
            for idx, m in enumerate(matches, 1):
                event_name = ((m.get("event") or {}).get("name") or "未知赛事").strip()
                stage = m.get("stage")
                stage_str = f" · {stage}" if stage else ""
                team1 = (m.get("team1") or {}).get("name") or "待定"
                team2 = (m.get("team2") or {}).get("name") or "待定"
                fmt = self.format_bo(m)
                m_id = m.get("id")

                time_str = "--:--"
                ts = self.parse_starts_at_ts(m)
                if ts:
                    dt_user = datetime.fromtimestamp(ts, tz=tz)
                    user_date = dt_user.strftime("%Y-%m-%d")
                    clock_str = dt_user.strftime("%H:%M")
                    # 如果在北京时间属于次日凌晨（例如 23:00 后过午夜 00:00/01:30）
                    if user_date > today_local_date and not is_next_day:
                        cst_time = f"次日 {clock_str}"
                    else:
                        cst_time = clock_str

                    # 动态获取比赛当地真实时区
                    m_tz = self.get_match_timezone(m)
                    if m_tz.key != tz.key:
                        dt_local = datetime.fromtimestamp(ts, tz=m_tz)
                        local_str = dt_local.strftime("%H:%M %Z")
                        time_str = f"{cst_time} (当地 {local_str})"
                    else:
                        time_str = cst_time

                id_suffix = f"  🆔 {m_id}" if m_id else ""
                lines.append(f"{idx}. ⏰ {time_str} | ({fmt})")
                lines.append(f"   🏆 {event_name}{stage_str}")
                lines.append(f"   ⚔️ {team1} 🆚 {team2}{id_suffix}")
                lines.append("")

            if lines[-1] == "":
                lines.pop()

        lines.append("------------------------------------")
        lines.append(f"📌 本比赛日共 {len(matches)} 场关注对决")
        lines.append("💡 发送 /hltv match <ID或战队> 查看全员KDA与Rating")
        lines.append("💡 发送 /hltv live 可随时查看实时赛况")
        return "\n".join(lines)

    def format_matchday_results(
        self,
        matches: List[Dict[str, Any]],
        matchday_str: str,
        is_today: bool = True,
        next_match_hint: Optional[str] = None,
    ) -> str:
        """格式化比赛日完赛赛果

        Args:
            matches: 完赛的大型比赛列表
            matchday_str: 比赛日字符串 (YYYY-MM-DD)
            is_today: 是否为当前比赛日
            next_match_hint: 若今日尚未完赛，提示今日首场开赛时间的文案
        """
        tz = self.get_tz()

        if is_today:
            header_title = f"🏁 【HLTV 今日关注赛果 (比赛日 {matchday_str})】"
        else:
            header_title = f"🏁 【HLTV 最近比赛日关注赛果 (比赛日 {matchday_str})】"

        lines = [
            header_title,
            "------------------------------------",
        ]

        if not matches:
            lines.append("该比赛日已标记的赛事暂无完赛记录。")
        else:
            for idx, m in enumerate(matches, 1):
                event_name = ((m.get("event") or {}).get("name") or "未知赛事").strip()
                team1 = (m.get("team1") or {}).get("name") or "队伍1"
                team2 = (m.get("team2") or {}).get("name") or "队伍2"
                s1, s2 = self.score_pair(m)
                fmt = self.format_bo(m)
                m_id = m.get("id")

                if s1 is None or s2 is None:
                    winner = "结果待定"
                else:
                    winner = team1 if s1 > s2 else (team2 if s2 > s1 else "平局")
                score_str = (
                    f"{s1 if s1 is not None else '-'} - {s2 if s2 is not None else '-'}"
                )
                url = m.get("url") or (
                    f"https://www.hltv.org/matches/{m_id}" if m_id else ""
                )

                # 比赛开打时间 (用户本地时区时间与比赛当地时间)
                time_str = ""
                ts = self.parse_starts_at_ts(m)
                if ts:
                    dt_user = datetime.fromtimestamp(ts, tz=tz)
                    cst_label = dt_user.strftime("%H:%M")
                    m_tz = self.get_match_timezone(m)
                    if m_tz.key != tz.key:
                        dt_local = datetime.fromtimestamp(ts, tz=m_tz)
                        local_label = dt_local.strftime("%H:%M %Z")
                        time_str = f" [{cst_label} (当地 {local_label})]"
                    else:
                        time_str = f" [{cst_label}]"

                lines.append(f"{idx}. 🏆 {event_name} ({fmt}){time_str}")
                lines.append(f"   ⚔️ {team1} {score_str} {team2} 🏆 {winner} 胜")
                if m_id:
                    lines.append(f"   🆔 比赛ID：{m_id}")
                if url:
                    lines.append(f"   🔗 {url}")
                lines.append("")

            if lines[-1] == "":
                lines.pop()

        lines.append("------------------------------------")
        lines.append(f"📌 共收录 {len(matches)} 场已完赛关注对决")
        if next_match_hint:
            lines.append(f"💡 {next_match_hint}")
        lines.append("💡 发送 /hltv match <ID或战队> 查看全员KDA与Rating")
        lines.append("💡 发送 /hltv today 查看最新关注赛程")
        return "\n".join(lines)

    async def check_daily_schedule(self, now: datetime) -> None:
        """检查并触发每日赛程推送"""
        if not self.config.get("daily_schedule_enabled", True):
            return

        target_time = self.config.get("daily_schedule_time", "09:00").strip()
        current_hm = now.strftime("%H:%M")
        current_matchday = self.get_current_matchday()

        if current_hm == target_time and self.last_daily_date != current_matchday:
            logger.info(
                f"[HLTV] 触发每日赛程定时推送: 比赛日 {current_matchday} {current_hm}"
            )
            self.last_daily_date = current_matchday
            self._save_state()

            if not self.get_tracked_event_ids():
                logger.info("[HLTV] 未标记任何赛事，跳过每日赛程推送。")
                return

            # 查询未来比赛并按比赛日聚合
            upcoming = await self.client.get_upcoming_matches(days=3, limit=300)
            today_major_matches: List[Dict[str, Any]] = []

            for m in upcoming:
                if not self.is_tracked_match(m):
                    continue
                ts = self.parse_starts_at_ts(m)
                if ts:
                    m_day = self.get_matchday(m, ts)
                    if m_day == current_matchday:
                        today_major_matches.append(m)

            today_major_matches.sort(key=lambda x: self.parse_starts_at_ts(x) or 0)
            msg = self.format_daily_schedule(today_major_matches, current_matchday)
            await self.broadcast_message(msg)

    async def check_match_reminders(self, now_ts: float) -> None:
        """检查未来 10 分钟内即将开赛的已标记赛事比赛并发送提醒"""
        if not self.get_tracked_event_ids():
            return
        upcoming = await self.client.get_upcoming_matches(days=1, limit=300)
        reminder_enabled = self.config.get("match_reminder_enabled", True)

        for match in upcoming:
            if not self.is_tracked_match(match):
                continue

            match_id = str(match.get("id") or "")
            if not match_id:
                continue

            starts_ts = self.parse_starts_at_ts(match)
            if not starts_ts:
                continue

            time_diff = starts_ts - now_ts

            # 赛前 10 分钟提醒区间：0 秒至 600 秒（10分钟）
            if 0 <= time_diff <= 600:
                if match_id not in self.reminded_match_ids:
                    self.reminded_match_ids.add(match_id)
                    logger.info(
                        f"[HLTV] 触发赛前10分钟提醒: 比赛ID {match_id} 即将在 {time_diff:.0f}s 后开赛"
                    )

                    if reminder_enabled:
                        msg = self.format_match_reminder(match)
                        await self.broadcast_message(msg)

                    # 自动加入赛后战报追踪队列
                    self._register_tracking_match(match, starts_ts, now_ts)
                    self._save_state()

            # 如果比赛已经开始（0 ~ 30分钟内）但之前未追踪，也加入战报追踪
            elif -1800 <= time_diff < 0:
                if (
                    match_id not in self.tracking_matches
                    and match_id not in self.reported_match_ids
                ):
                    self._register_tracking_match(match, starts_ts, now_ts)
                    self._save_state()

    def _register_tracking_match(
        self, match: Dict[str, Any], starts_ts: float, now_ts: float
    ) -> None:
        """将比赛登记到赛后战报轮询追踪队列"""
        match_id = str(match.get("id"))
        if match_id in self.tracking_matches or match_id in self.reported_match_ids:
            return

        fmt = match.get("format")
        delay_min = self.get_format_delay_minutes(fmt)
        delay_sec = delay_min * 60

        # 计算首次查询战报的时间
        first_check_ts = max(starts_ts + delay_sec, now_ts + 60)

        self.tracking_matches[match_id] = {
            "match_id": match_id,
            "format": fmt,
            "starts_at_ts": starts_ts,
            "next_check_ts": first_check_ts,
            "retry_count": 0,
            "team1": (match.get("team1") or {}).get("name"),
            "team2": (match.get("team2") or {}).get("name"),
            "event": (match.get("event") or {}).get("name"),
        }
        logger.info(
            f"[HLTV] 登记赛后战报追踪: 比赛 {match_id} ({fmt}), 预计在开赛后 {delay_min} 分钟 "
            f"({datetime.fromtimestamp(first_check_ts, tz=self.get_tz()).strftime('%H:%M')}) 首次获取战报"
        )

    async def check_tracking_results(self, now_ts: float) -> None:
        """检查正在追踪的比赛，到达时间后获取战报与数据"""
        report_enabled = self.config.get("result_report_enabled", True)
        retry_interval_min = int(self.config.get("result_retry_interval", 10))
        max_retries = int(self.config.get("max_result_retries", 30))

        match_ids = list(self.tracking_matches.keys())

        for match_id in match_ids:
            info = self.tracking_matches.get(match_id)
            if not info:
                continue

            next_check_ts = info.get("next_check_ts", 0)
            if now_ts < next_check_ts:
                continue

            logger.info(f"[HLTV] 到达战报获取时间，开始查询比赛详情: {match_id} ...")
            detail = await self.client.get_match_detail(match_id)

            if not detail:
                logger.warning(f"[HLTV] 无法获取比赛详情 {match_id}，稍后重试")
                info["retry_count"] = info.get("retry_count", 0) + 1
                info["next_check_ts"] = now_ts + retry_interval_min * 60
                self._save_state()
                continue

            status = detail.get("status")
            if status == "finished":
                logger.info(f"[HLTV] 比赛 {match_id} 已完赛，生成并推送战报！")
                if report_enabled:
                    msg = self.format_match_result(detail)
                    await self.broadcast_message(msg)

                self.reported_match_ids.add(match_id)
                self.tracking_matches.pop(match_id, None)
                self._save_state()
            else:
                retry_count = info.get("retry_count", 0) + 1
                info["retry_count"] = retry_count

                if retry_count >= max_retries:
                    logger.warning(
                        f"[HLTV] 比赛 {match_id} 超过最大重试次数 ({max_retries})，停止追踪。"
                    )
                    self.tracking_matches.pop(match_id, None)
                    self._save_state()
                else:
                    info["next_check_ts"] = now_ts + retry_interval_min * 60
                    logger.info(
                        f"[HLTV] 比赛 {match_id} 状态为 {status}，尚未完赛。第 {retry_count} 次重试，"
                        f"将在 {retry_interval_min} 分钟后再次检查。"
                    )
                    self._save_state()

    async def _loop(self) -> None:
        """主循环，每 30 秒轮询一次"""
        logger.info("[HLTV] 调度器主轮询循环已开启。")
        last_match_poll_ts = 0.0

        while self._running:
            try:
                now = datetime.now(tz=self.get_tz())
                now_ts = now.timestamp()

                # 1. 每日固定时间赛程推送检查
                await self.check_daily_schedule(now)

                # 2. 赛前提醒检查（每 60 秒拉取一次接口进行比对）
                if now_ts - last_match_poll_ts >= 60.0:
                    last_match_poll_ts = now_ts
                    await self.check_match_reminders(now_ts)

                # 3. 赛后战报结果追踪
                await self.check_tracking_results(now_ts)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[HLTV] 调度循环异常: {e}", exc_info=True)

            await asyncio.sleep(30)
