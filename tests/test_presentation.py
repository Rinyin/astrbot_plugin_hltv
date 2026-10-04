import asyncio
import importlib.util
import io
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from aiohttp import web
from PIL import Image


def load_modules():
    root = Path(__file__).parents[1]
    package = types.ModuleType("hltv_presentation_test")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    previous = sys.modules.get("astrbot.api")
    if previous is None:
        sys.modules["astrbot.api"] = types.SimpleNamespace(logger=logging.getLogger())
    result = []
    try:
        for name in ("assets", "presentation"):
            spec = importlib.util.spec_from_file_location(
                f"{package.__name__}.{name}", root / f"{name}.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            result.append(module)
    finally:
        if previous is None:
            sys.modules.pop("astrbot.api", None)
    return result


assets, presentation = load_modules()


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(stream, format="PNG")
    return stream.getvalue()


class AssetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = assets.AssetCache(self.temporary.name)
        self.requests = []
        self.bad = False
        self.block = None

        async def serve(request):
            self.requests.append(dict(request.headers))
            if self.block:
                await self.block.wait()
            if self.bad:
                return web.Response(text="<html>bad gateway</html>")
            if request.headers.get("If-None-Match") == '"v1"':
                return web.Response(status=304)
            return web.Response(body=png(), headers={"ETag": '"v1"'})

        app = web.Application()
        app.router.add_get("/{name}", serve)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/image"

    async def asyncTearDown(self):
        if self.block:
            self.block.set()
        await self.cache.close()
        await self.runner.cleanup()
        self.temporary.cleanup()

    async def test_concurrent_requests_download_once_and_restart_reuses(self):
        results = await asyncio.gather(
            *(self.cache.get("team", 1, self.url) for _ in range(5))
        )
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all(result == results[0] for result in results))
        await self.cache.close()
        self.cache = assets.AssetCache(self.temporary.name)
        self.assertEqual(await self.cache.get("team", 1, self.url), results[0])
        self.assertEqual(len(self.requests), 1)

    async def test_stale_returns_immediately_then_conditional_refresh(self):
        original = await self.cache.get("team", 1, self.url)
        self.cache.index["team:1"]["checked"] = 0
        self.block = asyncio.Event()
        self.assertEqual(
            await asyncio.wait_for(self.cache.get("team", 1, self.url), 0.2), original
        )
        self.block.set()
        await asyncio.gather(*list(self.cache._tasks.values()))
        self.assertEqual(self.requests[-1]["If-None-Match"], '"v1"')
        self.assertGreater(self.cache.index["team:1"]["checked"], 0)

    async def test_cold_nonblocking_lookup_downloads_once_and_later_reuses(self):
        self.block = asyncio.Event()
        results = await asyncio.wait_for(
            asyncio.gather(
                *(self.cache.get("team", 1, self.url, wait=False) for _ in range(12))
            ),
            1,
        )
        self.assertEqual(results, [None] * 12)
        self.assertEqual(len(self.cache._tasks), 1)
        self.block.set()
        await asyncio.gather(*list(self.cache._tasks.values()))
        self.assertTrue(await self.cache.get("team", 1, self.url, wait=False))
        self.assertEqual(len(self.requests), 1)

    async def test_url_change_failure_keeps_old_asset_and_cools(self):
        original = await self.cache.get("team", 1, self.url)
        self.bad = True
        self.assertEqual(await self.cache.get("team", 1, self.url + "2"), original)
        await asyncio.gather(*list(self.cache._tasks.values()))
        self.assertEqual(await self.cache.get("team", 1, self.url + "2"), original)
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("If-None-Match", self.requests[-1])

    async def test_age_does_not_delete_but_capacity_prunes_oldest(self):
        await self.cache.get("team", 1, self.url)
        first = self.cache.index["team:1"]["file"]
        self.cache.index["team:1"].update(checked=0, accessed=0)
        self.cache.prune()
        self.assertTrue((self.cache.directory / first).exists())
        await self.cache.get("team", 2, self.url)
        second = self.cache.index["team:2"]["file"]
        self.cache.max_bytes = len(png())
        self.cache.prune(protected={second})
        self.assertFalse((self.cache.directory / first).exists())
        self.assertTrue((self.cache.directory / second).exists())

    async def test_corruption_refetches(self):
        await self.cache.get("team", 1, self.url)
        (self.cache.directory / self.cache.index["team:1"]["file"]).write_text("bad")
        self.assertTrue(await self.cache.get("team", 1, self.url))
        self.assertEqual(len(self.requests), 2)

    async def test_omitted_url_still_reuses_cached_entity(self):
        original = await self.cache.get("team", 1, self.url)
        self.assertEqual(await self.cache.get("team", 1, None), original)
        self.assertEqual(len(self.requests), 1)

    async def test_prefetch_warms_once_and_get_reuses_it(self):
        self.assertTrue(await self.cache.prefetch("team", 7, self.url))
        self.assertEqual(len(self.requests), 1)
        image = await self.cache.get("team", 7, self.url)
        self.assertTrue(image.startswith("data:image/png"))
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(await self.cache.prefetch("team", 7, self.url))

    async def test_prefetch_skips_local_override_and_missing_url(self):
        path = Path(self.temporary.name) / "local" / "player" / "5.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(png("green"))
        self.assertTrue(await self.cache.prefetch("player", 5, self.url))
        self.assertFalse(await self.cache.prefetch("team", 8, None))
        self.assertEqual(self.requests, [])

    async def test_prefetch_reports_cold_download_failure(self):
        self.bad = True
        self.assertFalse(await self.cache.prefetch("team", 3, self.url))
        self.assertEqual(len(self.requests), 1)
        # The failure is cooling down and must not be retried immediately.
        self.assertFalse(await self.cache.prefetch("team", 3, self.url))
        self.assertEqual(len(self.requests), 1)

    async def test_prefetch_redownloads_when_cache_file_deleted(self):
        self.assertTrue(await self.cache.prefetch("team", 1, self.url))
        await asyncio.gather(*list(self.cache._tasks.values()))
        self.assertEqual(len(self.requests), 1)
        (self.cache.directory / self.cache.index["team:1"]["file"]).unlink()
        self.assertTrue(await self.cache.prefetch("team", 1, self.url))
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("If-None-Match", self.requests[1])
        self.assertTrue(await self.cache.get("team", 1, self.url))

    def test_svg_validation(self):
        self.assertEqual(
            assets.image_mime(
                b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
            ),
            "image/svg+xml",
        )
        self.assertEqual(
            assets.image_mime(
                b'<svg><defs><linearGradient id="a"/></defs><path fill="url(#a)"/><use href="#a"/></svg>'
            ),
            "image/svg+xml",
        )
        for unsafe in (
            b"<svg><script>alert(1)</script></svg>",
            b'<svg><style>@import "http://evil";</style></svg>',
            b"<html>error</html>",
        ):
            with self.assertRaises(Exception):
                assets.image_mime(unsafe)


def match():
    return {
        "id": "1",
        "status": "finished",
        "event": {"id": "9", "name": "赛事"},
        "team1": {"id": "10", "name": "Alpha"},
        "team2": {"id": "20", "name": "Beta"},
        "score": {"team1": 2, "team2": 0},
        "maps": [
            {
                "name": "Inferno",
                "team1_score": 13,
                "team2_score": 0,
                "half_scores": ["12:0", "1:0"],
            }
        ],
        "lineups": [
            {
                "team": {"id": team, "name": name},
                "players": [
                    {
                        "player_id": str(i),
                        "nickname": f"p{i}",
                        "photo": f"https://example.org/{i}.png",
                    }
                    for i in range(start, start + 5)
                ],
            }
            for team, name, start in [("10", "Alpha", 0), ("20", "Beta", 5)]
        ],
        "player_stats": [
            {
                "player_id": str(i),
                "nickname": f"p{i}",
                "team": "stale name",
                "rating": 1.0,
                "adr": 0,
                "kast": "0%",
                "kd_diff": 0,
                "kills_deaths": "0-0",
            }
            for i in range(10)
        ],
        "map_player_stats": {
            "Inferno": [{"player_id": "9", "nickname": "map_only", "rating": 0}]
        },
    }


class PresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        scheduler = Mock()
        scheduler.parse_starts_at_ts.return_value = 1780000000
        scheduler.get_match_timezone.return_value = ZoneInfo("Europe/Berlin")
        scheduler.get_matchday.return_value = "2026-05-28"
        scheduler.format_bo.return_value = "BO3"
        scheduler.format_match_detail.return_value = "match text"
        scheduler.format_daily_schedule.return_value = "schedule text"
        scheduler.delivery.protected_images = set()
        self.service = presentation.PresentationService(
            {},
            self.temporary.name,
            AsyncMock(return_value=png()),
            types.SimpleNamespace(get_event=AsyncMock(return_value={})),
            scheduler,
        )
        self.service.assets.get = AsyncMock(return_value=None)
        self.service.render_html = Mock(return_value="<html></html>")
        self.service._local = AsyncMock(side_effect=RuntimeError("browser unavailable"))
        self.addAsyncCleanup(self.service.close)

    def test_id_grouping_all_ten_players_and_zero_values(self):
        model = self.service._model(match())
        self.assertEqual([len(g["players"]) for g in model["groups"]], [5, 5])
        player = model["groups"][0]["players"][0]
        self.assertEqual(player["adr"], "0.0")
        self.assertEqual(player["kast"], "0%")
        self.assertEqual(player["difference"], "+0")
        self.assertEqual(model["score"], ["2", "0"])

    def test_map_never_uses_overall_statistics(self):
        model = self.service._model(match(), "1")
        self.assertEqual(model["score"], ["13", "0"])
        self.assertEqual(model["groups"][0]["players"][0]["name"], "map_only")
        self.assertEqual(model["maps"][0]["half"], "12:0 / 1:0")
        self.assertEqual(
            self.service._model(match(), "de_inferno")["map_name"], "Inferno"
        )

    async def test_local_failure_t2i_success_and_double_failure_text(self):
        page = (await self.service.build("match", [match()], "fallback"))[0]
        self.assertTrue(Path(page["image"]).is_file())
        self.service.html_render.assert_awaited_once_with(
            "{{ content | safe }}",
            {"content": "<html></html>"},
            return_url=False,
            options={"type": "png", "quality": None, "full_page": True},
        )
        self.service.html_render.side_effect = RuntimeError("t2i unavailable")
        page = (await self.service.build("match", [match()], "fallback"))[0]
        self.assertIsNone(page["image"])
        self.assertEqual(page["text"], "fallback")

    async def test_schedule_groups_events_and_pages_at_six(self):
        fixtures = [match() for _ in range(8)]
        fixtures[-1]["event"] = {"id": "11", "name": "Another event"}
        self.service._render = AsyncMock(return_value=None)
        pages = await self.service.build("schedule", fixtures, "all text")
        self.assertEqual(len(pages), 3)
        counts = [len(call.args[0]) for call in self.service.render_html.call_args_list]
        self.assertEqual(counts, [6, 1, 1])

    async def test_cold_assets_and_event_do_not_delay_first_page(self):
        gate = asyncio.Event()

        async def download(*args, **kwargs):
            await gate.wait()
            return png(), {}

        async def event(*args):
            await gate.wait()
            return {"header_image": "https://example.org/event.png"}

        await self.service.assets.close()
        self.service.assets = assets.AssetCache(
            Path(self.temporary.name) / "cold", download_asset=download
        )
        self.service.client.get_event = AsyncMock(side_effect=event)
        self.service._render = AsyncMock(return_value=None)
        pages = self.service.iter_pages("schedule", [match() for _ in range(7)], "text")
        try:
            first = await asyncio.wait_for(anext(pages), 1)
            self.assertIn("1/2", first["caption"])
            self.service._render.assert_awaited_once()
            self.assertTrue(self.service.assets._tasks)
            self.assertTrue(self.service._event_tasks)
            gate.set()
            await asyncio.gather(*list(self.service.assets._tasks.values()))
            await asyncio.gather(*list(self.service._event_tasks.values()))
            await asyncio.wait_for(anext(pages), 1)
            models = self.service.render_html.call_args.args[0]
            self.assertTrue(models[0]["groups"][0]["players"][0]["image"])
            self.service.client.get_event.assert_awaited_once()
        finally:
            gate.set()
            await pages.aclose()

    async def test_prefetch_dedupes_shared_assets_and_honors_limit(self):
        self.service.assets.prefetch = AsyncMock(return_value=True)

        def warm(match_id):
            item = match()
            item["id"] = match_id
            item["team1"]["logo"] = "https://example.org/alpha.png"
            item["team2"]["logo"] = "https://example.org/beta.png"
            return item

        await self.service.prefetch_assets([warm("1"), warm("2")], pause=0)
        calls = [
            (call.args[0], call.args[1])
            for call in self.service.assets.prefetch.await_args_list
        ]
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(self.service.assets.prefetch.await_count, 12)
        self.assertIn(("team", "10"), calls)
        self.assertIn(("player", "9"), calls)

        self.service.assets.prefetch.reset_mock()
        await self.service.prefetch_assets([warm("3")], limit=4, pause=0)
        self.assertEqual(self.service.assets.prefetch.await_count, 4)

    async def test_prefetch_raises_when_asset_fails(self):
        self.service.assets.prefetch = AsyncMock(return_value=False)
        with self.assertRaises(RuntimeError):
            await self.service.prefetch_assets([match()], pause=0)

    async def test_prefetch_raises_on_partial_roster_failure(self):
        self.service.assets.prefetch = AsyncMock(return_value=True)
        self.service.client.get_team = AsyncMock(
            side_effect=lambda team_id: (
                {"roster": [{"player_id": "1", "photo": "https://x/1.png"}]}
                if team_id == "10"
                else None
            )
        )
        with self.assertRaises(RuntimeError):
            await self.service.prefetch_assets([match()], pause=0, roster_pause=0)

    async def test_prefetch_skips_when_images_disabled(self):
        self.service.assets.prefetch = AsyncMock(return_value=True)
        self.service.config["image_enabled"] = False
        self.assertEqual(await self.service.prefetch_assets([match()]), 0)
        self.service.assets.prefetch.assert_not_awaited()

    async def test_prefetch_rosters_dedupe_and_skip_benched(self):
        self.service.assets.prefetch = AsyncMock(return_value=True)
        rosters = {
            "10": {
                "roster": [
                    {
                        "player_id": "101",
                        "photo": "https://example.org/101.png",
                        "status": "STARTER",
                    },
                    {
                        "player_id": "102",
                        "photo": "https://example.org/102.png",
                        "status": "BENCHED",
                    },
                    {"player_id": "103", "photo": "https://example.org/103.png"},
                ]
            },
            "20": {
                "roster": [
                    {
                        "player_id": "201",
                        "photo": "https://example.org/201.png",
                        "status": "STARTER",
                    }
                ]
            },
        }
        self.service.client.get_team = AsyncMock(
            side_effect=lambda team_id: rosters.get(team_id)
        )
        await self.service.prefetch_assets(
            [match(), match()], pause=0, roster_pause=0, matchday="2026-10-03"
        )
        self.assertEqual(
            [call.args[0] for call in self.service.client.get_team.await_args_list],
            ["10", "20"],
        )
        players = {
            str(call.args[1])
            for call in self.service.assets.prefetch.await_args_list
            if call.args[0] == "player"
        }
        self.assertTrue({"101", "103", "201"} <= players)
        self.assertNotIn("102", players)
        # A retry for the same push day reuses the cached rosters.
        self.service.client.get_team.reset_mock()
        await self.service.prefetch_assets(
            [match()], pause=0, roster_pause=0, matchday="2026-10-03"
        )
        self.service.client.get_team.assert_not_awaited()

    async def test_disabled_image_does_not_render(self):
        self.service.config["image_enabled"] = False
        self.assertEqual(
            await self.service.build("match", [match()], "fallback"),
            [{"text": "fallback", "caption": "", "image": None}],
        )
        self.service.render_html.assert_not_called()

    async def test_results_hint_only_on_last_page(self):
        self.service._render = AsyncMock(return_value=None)
        pages = await self.service.build(
            "results", [match() for _ in range(7)], "战报\n💡 今日首场将在 20:00 开赛"
        )
        self.assertNotIn("首场", pages[0]["caption"])
        self.assertIn("首场", pages[-1]["caption"])
        self.assertIn("首场", pages[-1]["text"])

    def test_cleanup_protects_pending_images(self):
        import os

        old = self.service.directory / "old.png"
        pending = self.service.directory / "pending.png"
        for path in (old, pending):
            path.write_bytes(png())
            os.utime(path, (time.time() - 90000,) * 2)
        self.service.scheduler.delivery.protected_images = {str(pending)}
        self.service.cleanup_rendered()
        self.assertFalse(old.exists())
        self.assertTrue(pending.exists())

    def test_template_escapes_dynamic_markup(self):
        models = [self.service._model(match())]
        models[0]["teams"][0]["name"] = "<script>boom</script>"
        html = self.service.environment.get_template("card.html").render(
            models=models, title="card", kind="match", font="", pages=1
        )
        self.assertIn("&lt;script&gt;boom&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)


if __name__ == "__main__":
    unittest.main()
