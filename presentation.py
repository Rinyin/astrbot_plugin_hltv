"""Shared image pages for commands and durable scheduled delivery."""

import asyncio
import base64
import io
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from astrbot.api import logger

from .assets import AssetCache, image_mime


def value(item, key, precision=None):
    result = item.get(key)
    if result is None or result == "":
        return "—"
    if precision is not None:
        try:
            return f"{float(result):.{precision}f}"
        except (TypeError, ValueError):
            return str(result)
    return str(result)


def selected_map(match, query):
    if not query:
        return None
    maps = match.get("maps") or []
    query = str(query).lower().removeprefix("de_")
    if query.isdigit() and 1 <= int(query) <= len(maps):
        return maps[int(query) - 1]
    for item in maps:
        if query in str(item.get("name", "")).lower().removeprefix("de_"):
            return item
    for name in match.get("map_player_stats") or {}:
        if query in name.lower().removeprefix("de_"):
            return {"name": name}
    raise ValueError("Map not found")


class PresentationService:
    def __init__(self, config, data_dir, html_render, client, scheduler):
        self.config = config
        self.directory = Path(data_dir) / "rendered"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.assets = AssetCache(
            Path(data_dir) / "assets",
            config.get("asset_refresh_days", 90),
            config.get("asset_cache_max_mb", 1024),
            download_asset=getattr(client, "get_asset", None),
        )
        self.html_render = html_render
        self.client = client
        self.scheduler = scheduler
        self.templates = Path(__file__).parent / "templates"
        self.environment = Environment(
            loader=FileSystemLoader(self.templates),
            autoescape=select_autoescape(["html"]),
        )
        self._browser = None
        self._playwright = None
        self._browser_lock = asyncio.Lock()
        self._render_limit = asyncio.Semaphore(2)
        self._cooldown = 0
        self._failures = 0
        self._event_cache = {}
        self._event_tasks = {}
        self._fonts = OrderedDict()
        self._build_tasks = set()
        self._closing = False

    def _time(self, match):
        timestamp = self.scheduler.parse_starts_at_ts(match)
        if not timestamp:
            return "开赛时间待定"
        china = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai"))
        local = datetime.fromtimestamp(
            timestamp, self.scheduler.get_match_timezone(match)
        )
        return f"北京时间 {china:%m-%d %H:%M} · 当地 {local:%m-%d %H:%M %Z}"

    def _model(self, match, map_name=None):
        teams = [dict(match.get("team1") or {}), dict(match.get("team2") or {})]
        chosen = selected_map(match, map_name)
        score = match.get("score") or {}
        if chosen:
            score = {
                "team1": chosen.get("team1_score"),
                "team2": chosen.get("team2_score"),
            }
        model = {
            "id": match.get("id", ""),
            "event": (match.get("event") or {}).get("name", "未知赛事"),
            "stage": match.get("stage") or "",
            "format": self.scheduler.format_bo(match),
            "status": "弃权赛"
            if match.get("forfeit")
            else {"upcoming": "未开赛", "live": "正在进行", "finished": "已结束"}.get(
                match.get("status"), "比赛"
            ),
            "live": match.get("status") == "live",
            "tracked": self.scheduler.is_tracked_match(match),
            "time": self._time(match),
            "matchday": self.scheduler.get_matchday(match),
            "teams": teams,
            "score": [value(score, "team1"), value(score, "team2")],
            "maps": [],
            "groups": [],
            "map_name": chosen.get("name") if chosen else None,
            "url": match.get("url")
            or f"https://www.hltv.org/matches/{match.get('id', '')}",
        }
        for item in [chosen] if chosen else match.get("maps") or []:
            if str(item.get("name", "")).upper() == "TBA":
                continue
            model["maps"].append(
                {
                    "name": item.get("name", "地图"),
                    "score": f"{value(item, 'team1_score')} : {value(item, 'team2_score')}",
                    "half": " / ".join(str(x) for x in item.get("half_scores") or []),
                    "unplayed": item.get("team1_score") is None
                    and item.get("team2_score") is None,
                }
            )
        stats = match.get("player_stats") or []
        if chosen:
            stats = next(
                (
                    v
                    for k, v in (match.get("map_player_stats") or {}).items()
                    if k.lower() == chosen.get("name", "").lower()
                ),
                [],
            )
        identities = {}
        names = {}
        for lineup in match.get("lineups") or []:
            team = lineup.get("team") or {}
            team_index = next(
                (
                    i
                    for i, t in enumerate(teams)
                    if (t.get("id") and str(t["id"]) == str(team.get("id")))
                    or (
                        t.get("name")
                        and t["name"].lower() == str(team.get("name", "")).lower()
                    )
                ),
                None,
            )
            for player in lineup.get("players") or []:
                if player.get("player_id") is not None:
                    identities[str(player["player_id"])] = (team_index, player)
                names[str(player.get("nickname", "")).lower()] = (team_index, player)
        groups = [[], [], []]
        for player in stats:
            index, identity = identities.get(
                str(player.get("player_id")),
                names.get(str(player.get("nickname", "")).lower(), (None, {})),
            )
            if index is None:
                team_name = player.get("team") or ""
                if isinstance(team_name, dict):
                    team_name = team_name.get("name", "")
                index = next(
                    (
                        i
                        for i, t in enumerate(teams)
                        if t.get("name", "").lower() == str(team_name).lower()
                    ),
                    2,
                )
            difference = player.get("kd_diff")
            groups[index].append(
                {
                    "id": player.get("player_id") or identity.get("player_id"),
                    "name": player.get("nickname") or "未知",
                    "photo": identity.get("photo") or player.get("photo"),
                    "kd": value(player, "kills_deaths"),
                    "difference": f"{difference:+d}"
                    if isinstance(difference, int)
                    else "—",
                    "positive": isinstance(difference, (int, float)) and difference > 0,
                    "adr": value(player, "adr", 1),
                    "kast": value(player, "kast"),
                    "rating": value(player, "rating", 2),
                    "rating_number": float(player.get("rating") or 0),
                }
            )
        for i, players in enumerate(groups):
            if players:
                players.sort(key=lambda p: p["rating_number"], reverse=True)
                model["groups"].append(
                    {
                        "name": teams[i].get("name", "待定") if i < 2 else "队伍未确认",
                        "players": players,
                    }
                )
        return model

    async def _enrich(self, models, matches):
        async def assign(target, field, kind, identity, url):
            target[field] = await self.assets.get(kind, identity, url, wait=False)

        calls = []
        for model, match in zip(models, matches):
            for team in model["teams"]:
                calls.append(
                    assign(team, "image", "team", team.get("id"), team.get("logo"))
                )
            for group in model["groups"]:
                for player in group["players"]:
                    calls.append(
                        assign(
                            player,
                            "image",
                            "player",
                            player.get("id"),
                            player.get("photo"),
                        )
                    )
        if calls:
            await asyncio.gather(*calls)

    async def _event_background(self, match):
        event = match.get("event") or {}
        event_id = str(event.get("id") or "")
        details = event
        if event_id and not event.get("header_image"):
            cached = self._event_cache.get(event_id)
            if not cached or time.time() - cached[0] > 86400:
                if event_id not in self._event_tasks and not self._closing:
                    task = asyncio.create_task(self._refresh_event(event_id, event))
                    self._event_tasks[event_id] = task
                    task.add_done_callback(
                        lambda done, key=event_id: self._event_tasks.pop(key, None)
                    )
            if cached:
                details = cached[1]
        return await self.assets.get(
            "event",
            event_id,
            details.get("header_image") or details.get("logo"),
            wait=False,
        )

    async def _refresh_event(self, event_id, event):
        try:
            fetched = await self.client.get_event(event_id)
            details = fetched or event
            self._event_cache[event_id] = (time.time(), details)
            await self.assets.get(
                "event",
                event_id,
                details.get("header_image") or details.get("logo"),
                wait=False,
            )
        except Exception:
            # Event decoration is optional; failed requests must not delay replies
            # or be retried on every page.
            self._event_cache[event_id] = (time.time() - 86400 + 300, event)
            logger.debug("[HLTV] 赛事背景预取失败: %s", event_id, exc_info=True)

    def render_html(self, models, title, kind, background=None, page=1, pages=1):
        template = self.environment.get_template("card.html")
        plain = template.render(
            models=models,
            title=title,
            kind=kind,
            background=None,
            page=page,
            pages=pages,
            font="",
        )
        # Subset against rendered text, excluding large embedded image data URIs.
        import re

        characters = "".join(sorted(set(re.sub(r"<[^>]*>", "", plain))))
        font_data = self._fonts.get(characters)
        if font_data is None:
            source = self.templates / "fonts" / "NotoSansSC.ttf"
            font_data = ""
            if source.exists():
                from fontTools import subset
                from fontTools.ttLib import TTFont

                with TTFont(source) as font:
                    options = subset.Options()
                    options.flavor = "woff2"
                    subsetter = subset.Subsetter(options=options)
                    subsetter.populate(text=characters)
                    subsetter.subset(font)
                    font.flavor = "woff2"
                    stream = io.BytesIO()
                    font.save(stream)
                    font_data = base64.b64encode(stream.getvalue()).decode()
            self._fonts[characters] = font_data
            while len(self._fonts) > 16:
                self._fonts.popitem(last=False)
        return template.render(
            models=models,
            title=title,
            kind=kind,
            background=background,
            page=page,
            pages=pages,
            font=font_data,
        )

    def cleanup_rendered(self):
        delivery = getattr(self.scheduler, "delivery", None)
        protected = {
            str(Path(p).resolve()) for p in getattr(delivery, "protected_images", set())
        }
        for path in self.directory.glob("*.png"):
            if (
                str(path.resolve()) not in protected
                and time.time() - path.stat().st_mtime > 86400
            ):
                path.unlink(missing_ok=True)

    async def _local(self, html, output):
        async with self._browser_lock:
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    channel="chromium", headless=True
                )
        page = await self._browser.new_page(
            viewport={"width": 1080, "height": 800}, device_scale_factor=1
        )
        try:
            await page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.url.startswith(("http:", "https:"))
                    else route.continue_()
                ),
            )
            await page.set_content(html, wait_until="load")
            await page.evaluate(
                "async () => { await document.fonts.ready; await Promise.all([...document.images].map(i => i.decode().catch(() => {}))); }"
            )
            await page.screenshot(path=str(output), full_page=True, type="png")
        finally:
            await page.close()

    async def _render(self, html):
        output = self.directory / f"{uuid.uuid4().hex}.png"
        backend = self.config.get("render_backend", "auto")
        async with self._render_limit:
            if backend in {"auto", "local"} and time.monotonic() >= self._cooldown:
                try:
                    await asyncio.wait_for(self._local(html, output), timeout=15)
                    self._failures = 0
                    return str(output)
                except Exception:
                    logger.warning("[HLTV] 本地图片渲染失败", exc_info=True)
                    self._failures += 1
                    if self._browser is None or self._failures >= 2:
                        self._cooldown = time.monotonic() + 300
            if backend in {"auto", "t2i"}:
                try:
                    rendered = await asyncio.wait_for(
                        self.html_render(
                            "{{ content | safe }}",
                            {"content": html},
                            return_url=False,
                            options={"type": "png", "quality": None, "full_page": True},
                        ),
                        timeout=20,
                    )
                    if isinstance(rendered, (bytes, bytearray)):
                        content = bytes(rendered)
                    else:
                        content = await asyncio.to_thread(Path(rendered).read_bytes)
                    await asyncio.to_thread(image_mime, content)
                    await asyncio.to_thread(output.write_bytes, content)
                    return str(output)
                except Exception:
                    logger.warning("[HLTV] T2I 渲染失败，回退文本", exc_info=True)
        output.unlink(missing_ok=True)
        return None

    def _fallback(self, kind, batch, text, title, map_name):
        if kind == "match":
            return self.scheduler.format_match_detail(batch[0], map_name)
        if kind == "reminder":
            return self.scheduler.format_match_reminder(batch[0])
        if kind == "schedule":
            return self.scheduler.format_daily_schedule(
                batch,
                self.scheduler.get_matchday(batch[0]),
                is_next_day="明日预告" in text,
            )
        # Page-specific results/live fallback without repeating the entire list.
        lines = [title or {"results": "近期战报", "live": "正在进行"}.get(kind, "HLTV")]
        for match in batch:
            score = match.get("score") or {}
            lines.extend(
                [
                    f"{'⭐ ' if kind == 'live' and self.scheduler.is_tracked_match(match) else ''}{(match.get('team1') or {}).get('name', '待定')} {value(score, 'team1')} - {value(score, 'team2')} {(match.get('team2') or {}).get('name', '待定')}",
                    self._time(match),
                    match.get("url")
                    or f"https://www.hltv.org/matches/{match.get('id', '')}",
                ]
            )
        return "\n".join(lines)

    async def _build_page(self, kind, batch, page, title, map_name, index, total):
        models = [self._model(match, map_name) for match in batch]
        background = None

        async def enrich():
            nonlocal background
            results = await asyncio.gather(
                self._enrich(models, batch),
                self._event_background(batch[0]),
                return_exceptions=True,
            )
            if isinstance(results[1], str):
                background = results[1]

        try:
            await asyncio.wait_for(enrich(), timeout=5)
        except TimeoutError:
            logger.debug("[HLTV] 素材准备超时，使用已有素材")
        label = title or {
            "match": "赛后战报" if batch[0].get("status") == "finished" else "比赛详情",
            "schedule": "明日赛程预告" if "明日预告" in page["text"] else "赛事日程",
            "reminder": "即将开赛",
            "results": "近期战报",
            "live": "正在进行",
        }.get(kind, "HLTV")
        html = await asyncio.to_thread(
            self.render_html, models, label, kind, background, index, total
        )
        page["image"] = await self._render(html)
        page["caption"] = (
            f"{label} · {models[0]['event']}"
            + (f" · {index}/{total}" if total > 1 else "")
            + "\n"
            + "\n".join(m["url"] for m in models)
        )

    async def build(self, kind, matches, text, *, title="", map_name=None):
        if self._closing:
            return [{"text": text, "caption": "", "image": None}]
        task = asyncio.create_task(
            self._build(kind, matches, text, title=title, map_name=map_name)
        )
        self._build_tasks.add(task)
        try:
            return await task
        finally:
            self._build_tasks.discard(task)

    async def _build(self, kind, matches, text, *, title="", map_name=None):
        return [
            page
            async for page in self.iter_pages(
                kind, matches, text, title=title, map_name=map_name
            )
        ]

    async def iter_pages(self, kind, matches, text, *, title="", map_name=None):
        """Render replies incrementally; scheduled delivery can still collect build()."""
        if self._closing:
            yield {"text": text, "caption": "", "image": None}
            return
        await asyncio.to_thread(self.cleanup_rendered)
        if not matches or not self.config.get("image_enabled", True):
            yield {"text": text, "caption": "", "image": None}
            return
        groups = OrderedDict()
        for match in matches:
            event = match.get("event") or {}
            key = str(event.get("id") or event.get("name") or "unknown")
            groups.setdefault(key, []).append(match)
        batches = [
            items[i : i + (1 if kind in {"match", "reminder"} else 6)]
            for items in groups.values()
            for i in range(0, len(items), 1 if kind in {"match", "reminder"} else 6)
        ]
        for index, batch in enumerate(batches, 1):
            if self._closing:
                return
            fallback = (
                text
                if len(batches) == 1
                else self._fallback(kind, batch, text, title, map_name)
            )
            page = {"text": fallback, "caption": "", "image": None}
            task = asyncio.create_task(
                self._build_page(
                    kind, batch, page, title, map_name, index, len(batches)
                )
            )
            self._build_tasks.add(task)
            try:
                await asyncio.wait_for(task, timeout=40)
            except Exception:
                logger.warning("[HLTV] 图片页面准备失败，使用文本", exc_info=True)
            finally:
                self._build_tasks.discard(task)
            if index == len(batches):
                hints = "\n".join(
                    line for line in text.splitlines() if line.startswith("💡")
                )
                if hints:
                    page["caption"] += "\n" + hints
                    if len(batches) > 1:
                        page["text"] += "\n" + hints
            yield page

    async def close(self):
        self._closing = True
        tasks = list(self._build_tasks) + list(self._event_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # One resource failing to close must not leak the remaining resources.
        for name, resource, method in (
            ("素材缓存", self.assets, "close"),
            ("浏览器", self._browser, "close"),
            ("Playwright", self._playwright, "stop"),
        ):
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception:
                    logger.warning("[HLTV] 关闭%s失败", name, exc_info=True)
        self._browser = None
        self._playwright = None
