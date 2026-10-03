import asyncio
import importlib
import logging
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from test_presentation import assets, png


def load_client():
    previous = sys.modules.get("astrbot.api")
    if previous is None:
        sys.modules["astrbot.api"] = types.SimpleNamespace(logger=logging.getLogger())
    try:
        return importlib.import_module("hltv_presentation_test.api").HLTVClient
    finally:
        if previous is None:
            sys.modules.pop("astrbot.api", None)


HLTVClient = load_client()


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.status = 200
        self.requests = []
        self.origin_requests = 0
        self.payload = png()

        async def proxy(request):
            self.requests.append((request.query["url"], dict(request.headers)))
            if self.status != 200:
                return web.Response(status=self.status)
            if request.headers.get("If-None-Match") == '"proxy-v1"':
                return web.Response(status=304, headers={"ETag": '"proxy-v1"'})
            return web.Response(body=self.payload, headers={"ETag": '"proxy-v1"'})

        async def origin(request):
            self.origin_requests += 1
            return web.Response(body=png("blue"))

        app = web.Application()
        app.router.add_get("/api/v1/assets", proxy)
        app.router.add_get("/original", origin)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.client = HLTVClient(self.base)
        self.cache = assets.AssetCache(
            self.temporary.name, download_asset=self.client.get_asset
        )

    async def asyncTearDown(self):
        await self.cache.close()
        await self.client.close()
        await self.runner.cleanup()
        self.temporary.cleanup()

    async def test_api_preserves_original_query_and_conditional_cache(self):
        url = self.base + "/original?name=a%2Fb&v=1&other=hello%20world"
        image = await self.cache.get("team", 1, url)
        self.assertTrue(image.startswith("data:image/png"))
        self.assertEqual(self.requests[0][0], url)
        self.assertEqual(self.origin_requests, 0)
        self.cache.index["team:1"]["checked"] = 0
        self.assertEqual(await self.cache.get("team", 1, url), image)
        await asyncio.gather(*list(self.cache._tasks.values()))
        self.assertEqual(self.requests[1][1]["If-None-Match"], '"proxy-v1"')
        self.assertEqual(self.origin_requests, 0)
        session = await self.client._get_session()
        await self.cache.close()
        self.assertFalse(session.closed)

    async def test_old_api_404_falls_back_to_original(self):
        self.status = 404
        self.assertIsNotNone(await self.cache.get("team", 1, self.base + "/original"))
        self.assertEqual(self.origin_requests, 1)

    async def test_503_keeps_stale_and_never_requests_origin(self):
        url = self.base + "/original"
        image = await self.cache.get("team", 1, url)
        self.cache.index["team:1"]["checked"] = 0
        self.status = 503
        self.assertEqual(await self.cache.get("team", 1, url), image)
        await asyncio.gather(*list(self.cache._tasks.values()))
        self.assertEqual(self.origin_requests, 0)
        self.assertEqual(await self.cache.get("team", 1, url), image)
        self.assertEqual(len(self.requests), 2)

    async def test_api_rejects_oversize_and_invalid_payload(self):
        self.payload = b"x" * (5 * 1024 * 1024 + 1)
        self.assertIsNone(await self.cache.get("team", 1, self.base + "/original"))
        self.payload = b"<html>blocked</html>"
        self.assertIsNone(await self.cache.get("team", 2, self.base + "/original"))
        self.assertEqual(self.origin_requests, 0)

    async def test_local_override_precedes_network_and_survives_pruning(self):
        path = Path(self.temporary.name) / "local" / "player" / "42.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(png("green"))
        self.cache._download_asset = AsyncMock(side_effect=AssertionError("network"))
        image = await self.cache.get("player", 42, None)
        self.assertTrue(image.startswith("data:image/png"))
        self.assertEqual(
            image, await self.cache.get("player", 42, "https://example.org/photo.png")
        )
        self.cache.max_bytes = 1
        self.cache.prune()
        self.assertTrue(path.exists())
        self.cache._download_asset.assert_not_awaited()

    async def test_image_validation_runs_off_loop_and_corruption_redownloads(self):
        event_loop_thread = threading.get_ident()
        threads = []
        original = assets.image_mime

        def validate(content):
            threads.append(threading.get_ident())
            return original(content)

        url = self.base + "/original"
        with patch.object(assets, "image_mime", validate):
            self.assertIsNotNone(await self.cache.get("team", 1, url))
            path = Path(self.temporary.name) / self.cache.index["team:1"]["file"]
            path.write_bytes(b"corrupt image")
            self.assertIsNotNone(await self.cache.get("team", 1, url))
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("If-None-Match", self.requests[1][1])
        self.assertTrue(threads)
        self.assertNotIn(event_loop_thread, threads)

    async def test_lookup_finishing_after_close_cannot_start_download(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def local(*args):
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=5)
            return None

        self.cache._local_override = local
        lookup = asyncio.create_task(self.cache.get("team", 1, self.base + "/original"))
        try:
            await started.wait()
            await self.cache.close()
        finally:
            release.set()
        self.assertIsNone(await lookup)
        self.assertIsNone(await self.cache.get("team", 2, self.base + "/original"))
        self.assertFalse(self.requests)
        self.assertIsNone(self.client._session)
        self.assertFalse(self.cache._tasks)

    async def test_close_cancels_and_drains_inflight_download(self):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def download(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        self.cache._download_asset = download
        lookup = asyncio.create_task(self.cache.get("team", 1, self.base + "/original"))
        await started.wait()
        await self.cache.close()
        with self.assertRaises(asyncio.CancelledError):
            await lookup
        self.assertTrue(finished.is_set())
        self.assertFalse(self.cache._tasks)
