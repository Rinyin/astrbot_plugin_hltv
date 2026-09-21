import asyncio
import socket
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp.resolver import ThreadedResolver

from astrbot.api import logger

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

    async def _get(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """发送 GET 请求并解析 JSON"""
        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        f"[HLTV] 请求 {url} 失败: 状态码 {resp.status}, 响应: {text[:200]}"
                    )
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

    async def get_upcoming_matches(
        self, days: int = 2, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取即将进行的比赛"""
        return await self.get_matches(status="upcoming", days=days, limit=limit)

    async def get_live_matches(self) -> List[Dict[str, Any]]:
        """获取正在进行的比赛"""
        return await self.get_matches(status="live", limit=50)

    async def get_match_detail(self, match_id: str) -> Optional[Dict[str, Any]]:
        """获取比赛详情（包含比分、地图、各选手统计数据等）"""
        data = await self._get(f"/api/v1/matches/{match_id}")
        if data and isinstance(data, dict):
            return data.get("match")
        return None

    async def get_results(
        self,
        days: Optional[int] = 7,
        limit: int = 100,
        event_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取最近完赛结果。传入 event_id 时仅返回该赛事的完赛记录（由上游按赛事过滤）"""
        params: Dict[str, Any] = {"limit": limit}
        if days is not None:
            params["days"] = days
        if event_id:
            params["event"] = str(event_id)
        data = await self._get("/api/v1/results", params=params)
        if data and isinstance(data, dict):
            return data.get("matches", [])
        return []

    async def get_events(
        self, status: str = "ongoing", limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取赛事列表。status: ongoing | upcoming | past | all"""
        data = await self._get(
            "/api/v1/events", params={"status": status, "limit": limit}
        )
        if data and isinstance(data, dict):
            return data.get("events", [])
        return []

    async def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """获取单个赛事详情（名称、日期、奖金池、参赛队等）"""
        data = await self._get(f"/api/v1/events/{event_id}")
        if data and isinstance(data, dict):
            return data.get("event")
        return None

    async def find_match(self, query: str) -> Optional[Dict[str, Any]]:
        """根据比赛ID或战队名称查找比赛详情"""
        clean_q = query.strip()
        if not clean_q:
            return None

        # 纯数字直接按比赛ID查询
        if clean_q.isdigit():
            return await self.get_match_detail(clean_q)

        q_low = clean_q.lower()

        # 1. 正在进行的比赛
        live_matches = await self.get_live_matches()
        for m in live_matches:
            t1 = ((m.get("team1") or {}).get("name") or "").lower()
            t2 = ((m.get("team2") or {}).get("name") or "").lower()
            if q_low in t1 or q_low in t2:
                m_id = m.get("id")
                if m_id:
                    detail = await self.get_match_detail(str(m_id))
                    if detail:
                        return detail
                return m

        # 2. 近期完赛结果
        results = await self.get_results(days=3, limit=50)
        for m in results:
            t1 = ((m.get("team1") or {}).get("name") or "").lower()
            t2 = ((m.get("team2") or {}).get("name") or "").lower()
            if q_low in t1 or q_low in t2:
                m_id = m.get("id")
                if m_id:
                    detail = await self.get_match_detail(str(m_id))
                    if detail:
                        return detail
                return m

        # 3. 未来比赛
        upcoming = await self.get_upcoming_matches(days=3, limit=50)
        for m in upcoming:
            t1 = ((m.get("team1") or {}).get("name") or "").lower()
            t2 = ((m.get("team2") or {}).get("name") or "").lower()
            if q_low in t1 or q_low in t2:
                m_id = m.get("id")
                if m_id:
                    detail = await self.get_match_detail(str(m_id))
                    if detail:
                        return detail
                return m

        return None
