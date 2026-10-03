"""Plugin unload must drain owned work and close every resource."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_commands import plugin_class  # noqa: F401
from test_presentation import presentation


@pytest.mark.asyncio
async def test_unload_cancels_and_awaits_active_render(tmp_path):
    service = presentation.PresentationService(
        {}, tmp_path, AsyncMock(), None, SimpleNamespace()
    )
    started = asyncio.Event()
    finished = asyncio.Event()

    async def render(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    service._build = render
    build = asyncio.create_task(service.build("match", [], "fallback"))
    await started.wait()
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await build
    assert finished.is_set()
    assert not service._build_tasks
    assert await service.build("match", [], "fallback") == [
        {"text": "fallback", "caption": "", "image": None}
    ]


@pytest.mark.asyncio
async def test_renderer_cleanup_continues_after_resource_failure(tmp_path):
    service = presentation.PresentationService(
        {}, tmp_path, AsyncMock(), None, SimpleNamespace()
    )
    service.assets.close = AsyncMock(side_effect=RuntimeError("cache failure"))
    browser = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("browser failure"))
    )
    playwright = SimpleNamespace(stop=AsyncMock())
    service._browser = browser
    service._playwright = playwright
    await service.close()
    browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert service._browser is None
    assert service._playwright is None


@pytest.mark.asyncio
async def test_plugin_cleanup_continues_after_service_failure(plugin_class):  # noqa: F811
    plugin = plugin_class.__new__(plugin_class)
    plugin.scheduler = SimpleNamespace(
        stop=AsyncMock(side_effect=RuntimeError("scheduler failure"))
    )
    plugin.presentation = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("renderer failure"))
    )
    plugin.client = SimpleNamespace(close=AsyncMock())
    await plugin.terminate()
    plugin.presentation.close.assert_awaited_once()
    plugin.client.close.assert_awaited_once()
