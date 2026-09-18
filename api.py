import asyncio
import logging
import socket
import time
from typing import Any, Dict, List, Optional, Set
import aiohttp
from aiohttp.resolver import ThreadedResolver

logger = logging.getLogger("astrbot")

DEFAULT_HOST_MAP = {
    "hltv.rinyin.top": "x.x.x.x",
}


class HostResolver(ThreadedResolver):
    """自定义 DNS 解析器，支持将特定域名直接解析为指定的服务器真实 IP"""

    def __init__(self, host_map: Optional[Dict[str, str]] = None):
        super().__init__()
        self.host_map = host_map or {}

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> List[Dict[str, Any]]:
        if host in self.host_map:
            target_ip = self.host_map[host]
            return [
                {
                    "hostname": host,
                    "host": target_ip,
                    "port": port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                }
            ]
        return await super().resolve(host, port, family)


# 知名 CS2 战队常见中文别名/简称映射表
TEAM_ALIASES: Dict[str, List[str]] = {
    "spirit": ["绿龙", "ts", "spirit", "team spirit"],
    "vitality": ["小蜜蜂", "蜜蜂", "薯条队", "vitality", "vit"],
    "faze": ["银河战舰", "脸皮", "大表哥队", "大表哥", "faze", "faze clan"],
    "natus vincere": ["navi", "邪剑仙", "小黑子", "天生赢家", "乌克兰队", "natus vincere"],
    "g2": ["g2", "武士", "马戏团", "呼吸队", "g2 esports"],
    "mouz": ["mouz", "老鼠", "耗子", "mousesports"],
    "astralis": ["a队", "红色星辰", "astralis", "ast"],
    "virtus.pro": ["vp", "大狗", "慢热队", "virtus.pro", "virtus pro"],
    "heroic": ["heroic", "英雄", "绿星"],
    "liquid": ["liquid", "液体", "小马", "team liquid"],
    "the mongolz": ["mongolz", "蒙古", "蒙古队", "the mongolz"],
    "furia": ["furia", "黑豹", "黑豹队"],
    "eternal fire": ["ef", "土耳其神车", "永恒之火", "eternal fire"],
    "complexity": ["col", "达拉斯牛仔", "狂热", "complexity"],
    "falcons": ["falcons", "猎鹰", "财阀", "team falcons"],
    "pain": ["pain", "痛苦", "pain gaming"],
    "ninjas in pyjamas": ["nip", "睡衣忍者", "忍者", "ninjas in pyjamas"],
    "cloud9": ["c9", "云九", "北美邪教", "cloud9"],
    "fnatic": ["fnatic", "宇宙队", "橙黑军团", "fnc"],
    "tyloo": ["tyloo", "天禄", "小龙"],
    "rare atom": ["ra", "稀有原子", "原子", "rare atom"],
    "lynn vision": ["lvg", "领航员", "飞人", "lynn vision"],
    "ence": ["ence", "芬兰冰刀"],
    "gamerlegion": ["gl", "玩家军团", "gamerlegion"],
    "aurora": ["aurora", "极光"],
    "betboom": ["bb", "bb队", "betboom"],
    "3dmax": ["3dmax", "3d"],
    "big": ["big", "大脑", "德国战车"],
    "mibr": ["mibr", "巴西人", "made in brazil"],
    "saw": ["saw", "电锯", "锯子"],
    "b8": ["b8", "8队", "八队"],
    "flyquest": ["flyquest", "飞客", "飞艇"],
    "legacy": ["legacy", "遗产"],
    "m80": ["m80"],
    "9z": ["9z", "9z队", "9z globant"],
    "imperial": ["imperial", "帝国"],
    "bestia": ["bestia", "野兽"],
    "parivision": ["pari", "parivision"],
    "nemiga": ["nemiga", "内米加"],
    "sangal": ["sangal", "桑加尔"],
    "sinners": ["sinners", "罪人"],
    "wildcard": ["wildcard", "通配符"],
    "red canids": ["red canids", "红犬"],
    "nrg": ["nrg"],
}


def resolve_team_aliases(query: str) -> List[str]:
    """将用户输入的别名或简称解析为对应的标准战队小写名称列表"""
    clean = query.strip().lower()
    canonical = []
    for canon, aliases in TEAM_ALIASES.items():
        if clean == canon:
            canonical.append(canon)
            continue
        for a in aliases:
            a_lower = a.lower()
            if len(clean) <= 3 or len(a_lower) <= 3:
                if clean == a_lower:
                    canonical.append(canon)
                    break
            else:
                if clean == a_lower or clean in a_lower or a_lower in clean:
                    canonical.append(canon)
                    break
    return list(dict.fromkeys(canonical))


class HLTVClient:
    """HLTV API 异步客户端封装"""

    def __init__(
        self,
        base_url: str = "https://hltv.rinyin.top",
        timeout: float = 15.0,
        server_ip: Optional[str] = "x.x.x.x",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.server_ip = server_ip
        self._session: Optional[aiohttp.ClientSession] = None
        self._cached_top_teams: Set[str] = set()
        self._top_teams_cached_at: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            host_map = dict(DEFAULT_HOST_MAP)
            if self.server_ip:
                host_map["hltv.rinyin.top"] = self.server_ip

            resolver = HostResolver(host_map=host_map)
            connector = aiohttp.TCPConnector(resolver=resolver)
            client_timeout = aiohttp.ClientTimeout(total=self.timeout)

            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=client_timeout,
                headers={
                    "User-Agent": "AstrBot-HLTV-Plugin/1.0",
                    "Accept": "application/json",
                },
            )
        return self._session

    async def close(self) -> None:
        """关闭客户端会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """发送 GET 请求并解析 JSON"""
        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"[HLTV] 请求 {url} 失败: 状态码 {resp.status}, 响应: {text[:200]}")
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            logger.warning(f"[HLTV] 请求 {url} 超时 ({self.timeout}s)")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"[HLTV] 网络请求错误 {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"[HLTV] 请求异常 {url}: {e}", exc_info=True)
            return None

    async def get_matches(
        self,
        status: str = "all",
        days: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取比赛列表
        
        Args:
            status: all | upcoming | live
            days: 未来 N 天内的比赛
            limit: 返回数量限制
            offset: 分页偏移量
        """
        params: Dict[str, Any] = {"status": status, "limit": limit, "offset": offset}
        if days is not None:
            params["days"] = days

        data = await self._get("/api/v1/matches", params=params)
        if data and isinstance(data, dict):
            return data.get("matches", [])
        return []

    async def get_upcoming_matches(self, days: int = 2, limit: int = 100) -> List[Dict[str, Any]]:
        """获取即将进行的比赛"""
        return await self.get_matches(status="upcoming", days=days, limit=limit)

    async def get_live_matches(self) -> List[Dict[str, Any]]:
        """获取正在进行的比赛"""
        return await self.get_matches(status="live", limit=50)

    async def clear_cache(self, prefix: Optional[str] = None) -> bool:
        """清除上游 API 服务的缓存，强制更新实时数据"""
        try:
            session = await self._get_session()
            url = f"{self.base_url}/api/v1/cache"
            params = {"prefix": prefix} if prefix else None
            async with session.delete(url, params=params) as resp:
                if resp.status == 200:
                    logger.info(f"[HLTV] 成功清除上游 API 缓存 (prefix={prefix})")
                    return True
        except Exception as e:
            logger.warning(f"[HLTV] 清除上游缓存失败: {e}")
        return False

    async def get_match_detail(self, match_id: str, auto_sync_results: bool = True) -> Optional[Dict[str, Any]]:
        """获取比赛详情（包含比分、地图、各选手统计数据等）。
        若上游返回半场陈旧缓存或单场接口受限(503)，自动与最新赛果库/赛程库同步并修复。
        """
        match_id_str = str(match_id)
        data = await self._get(f"/api/v1/matches/{match_id_str}")
        detail = data.get("match") if (data and isinstance(data, dict)) else None

        # 1. 若获取到了 match 数据，检查是否是陈旧数据（例如比赛已完赛但缓存中仍是 live/半场）
        if detail and auto_sync_results:
            st = detail.get("status")
            # 若状态不是 finished，或者地图数据不全，尝试到 results 中验证是否已实际完赛
            if st != "finished":
                try:
                    results = await self.get_results(days=7, limit=100)
                    for r in results:
                        if str(r.get("id")) == match_id_str:
                            logger.info(f"[HLTV] 检测到比赛 {match_id_str} 详情数据陈旧(原状态: {st})，已完赛，自动同步赛果！")
                            detail["status"] = "finished"
                            if r.get("score"):
                                detail["score"] = r.get("score")
                            if r.get("winner"):
                                detail["winner"] = r.get("winner")
                            detail["is_synced_from_results"] = True
                            break
                except Exception as e:
                    logger.debug(f"[HLTV] 自动对齐完赛结果跳过: {e}")

            return detail

        # 2. 若单场详情接口受限(如 503)或返回空，从赛果库与赛程库中智能回退构建比赛数据
        try:
            results = await self.get_results(days=7, limit=100)
            for r in results:
                if str(r.get("id")) == match_id_str:
                    logger.info(f"[HLTV] 单场接口未响应，从赛果列表成功匹配到比赛 {match_id_str}")
                    r_copy = dict(r)
                    r_copy["status"] = "finished"
                    r_copy["player_stats"] = []
                    r_copy["maps"] = []
                    r_copy["is_fallback_from_list"] = True
                    return r_copy
        except Exception:
            pass

        try:
            upcoming = await self.get_matches(status="all", days=7, limit=100)
            for m in upcoming:
                if str(m.get("id")) == match_id_str:
                    logger.info(f"[HLTV] 单场接口未响应，从赛程列表成功匹配到比赛 {match_id_str}")
                    m_copy = dict(m)
                    m_copy["player_stats"] = []
                    m_copy["maps"] = []
                    m_copy["is_fallback_from_list"] = True
                    return m_copy
        except Exception:
            pass

        return None

    async def get_top_teams(self, max_age_seconds: float = 21600.0) -> Set[str]:
        """获取当前 HLTV 世界排名前 30 的战队名称（小写集合，带缓存）"""
        now = time.time()
        if self._cached_top_teams and (now - self._top_teams_cached_at < max_age_seconds):
            return self._cached_top_teams

        teams: Set[str] = set()
        fallback_teams = {
            "spirit", "falcons", "mouz", "furia", "vitality", "legacy", "fut", "g2",
            "aurora", "natus vincere", "navi", "astralis", "betboom", "faze", "9z",
            "the mongolz", "mongolz", "b8", "magic", "mibr", "parivision", "gamerlegion",
            "alliance", "liquid", "3dmax", "inner circle", "big", "m80", "pain", "hotu",
            "ninjas in pyjamas", "nip", "jijiehao", "heroic", "complexity", "virtus.pro",
            "vp", "cloud9", "ence", "fnatic", "saw"
        }
        teams.update(fallback_teams)

        try:
            data = await self._get("/api/v1/rankings/teams")
            if data and isinstance(data, dict):
                rankings = data.get("rankings") or data.get("teams") or []
                for r in rankings:
                    t_info = r.get("team") if isinstance(r, dict) else None
                    if isinstance(t_info, dict):
                        t_name = t_info.get("name")
                        if t_name:
                            teams.add(t_name.strip().lower())
                    elif isinstance(r, dict) and "name" in r:
                        teams.add(r["name"].strip().lower())
            if teams:
                self._cached_top_teams = teams
                self._top_teams_cached_at = now
                logger.debug(f"[HLTV] 成功更新世界排名战队缓存，共 {len(teams)} 支强队")
        except Exception as e:
            logger.warning(f"[HLTV] 拉取世界排名战队失败，使用保底名单: {e}")

        return self._cached_top_teams or fallback_teams

    async def get_results(self, days: Optional[int] = 7, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近完赛结果"""
        params: Dict[str, Any] = {"limit": limit}
        if days is not None:
            params["days"] = days
        data = await self._get("/api/v1/results", params=params)
        if data and isinstance(data, dict):
            return data.get("matches", [])
        return []

    async def find_match(self, query: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """根据比赛ID或战队名称/别名查找比赛详情（支持强刷与别名识别）"""
        clean_q = query.strip()
        if not clean_q:
            return None

        # 若要求强制刷新，先清除服务端缓存
        if force_refresh:
            await self.clear_cache()

        # 1. 纯数字直接按比赛ID查询
        if clean_q.isdigit():
            return await self.get_match_detail(clean_q)

        q_low = clean_q.lower()
        canonical_targets = resolve_team_aliases(clean_q)

        def match_team(team_obj: Optional[Dict[str, Any]]) -> bool:
            if not team_obj or not isinstance(team_obj, dict):
                return False
            name = (team_obj.get("name") or "").strip().lower()
            if not name:
                return False
            if q_low in name or name in q_low:
                return True
            for tgt in canonical_targets:
                if tgt == name or tgt in name or name in tgt:
                    return True
            return False

        # 2. 检查正在进行的比赛
        live_matches = await self.get_live_matches()
        for m in live_matches:
            if match_team(m.get("team1")) or match_team(m.get("team2")):
                m_id = m.get("id")
                if m_id:
                    detail = await self.get_match_detail(str(m_id))
                    if detail:
                        return detail
                return m

        # 3. 检查近期完赛结果 (默认 7 天)
        results = await self.get_results(days=7, limit=100)
        for m in results:
            if match_team(m.get("team1")) or match_team(m.get("team2")):
                m_id = m.get("id")
                if m_id:
                    detail = await self.get_match_detail(str(m_id))
                    if detail:
                        return detail
                return m

        # 4. 检查未来赛程 (默认 7 天)
        upcoming = await self.get_upcoming_matches(days=7, limit=100)
        for m in upcoming:
            if match_team(m.get("team1")) or match_team(m.get("team2")):
                m_id = m.get("id")
                if m_id:
                    detail = await self.get_match_detail(str(m_id))
                    if detail:
                        return detail
                return m

        return None
