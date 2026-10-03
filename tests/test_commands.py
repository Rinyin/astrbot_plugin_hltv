"""Command integration tests with an isolated, non-network AstrBot surface."""

import importlib
import logging
import sys
from pathlib import Path
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    def decorator(*args, **kwargs):
        def decorate(function):
            function.command = decorator
            return function

        return decorate

    class Image:
        @staticmethod
        def fromFileSystem(path):
            return ("image", path)

    class Star:
        def __init__(self, context):
            self.context = context

    class MessageChain:
        def __init__(self):
            self.parts = []

        def file_image(self, path):
            self.parts.append(("image", path))
            return self

        def message(self, text):
            self.parts.append(("text", text))
            return self

    modules = {
        "astrbot": {},
        "astrbot.api": {
            "logger": logging.getLogger("hltv-test"),
            "AstrBotConfig": dict,
        },
        "astrbot.api.event": {
            "AstrMessageEvent": object,
            "MessageChain": MessageChain,
            "filter": SimpleNamespace(
                command_group=decorator,
                permission_type=decorator,
                PermissionType=SimpleNamespace(ADMIN="admin"),
            ),
        },
        "astrbot.api.star": {
            "Context": object,
            "Star": Star,
            "register": decorator,
            "StarTools": SimpleNamespace(get_data_dir=lambda *args: tmp_path),
        },
        "astrbot.api.message_components": {
            "Image": Image,
            "Plain": lambda text: ("plain", text),
        },
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    package_name = "_hltv_command_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    monkeypatch.setitem(sys.modules, package_name, package)
    # Ensure per-test mocks do not leak into any real plugin import.
    for name in list(sys.modules):
        if name.startswith(package_name + "."):
            monkeypatch.delitem(sys.modules, name)
    return importlib.import_module(package_name + ".main").HLTVPlugin


@pytest.fixture
def plugin(plugin_class):
    instance = plugin_class.__new__(plugin_class)
    instance.config = {"tracked_events": ["1"]}
    instance.scheduler = SimpleNamespace(
        get_tracked_event_ids=lambda: ["1"],
        get_current_matchday=lambda: "2026-10-03",
        format_daily_schedule=lambda matches, day, **kwargs: "Schedule " + day,
        format_matchday_results=lambda matches, day, **kwargs: "Results " + day,
        format_match_detail=lambda detail, **kwargs: "Match details",
        parse_starts_at_ts=lambda match: match.get("timestamp", 1),
        get_matchday=lambda match, ts: match["day"],
        get_match_timezone=lambda match: ZoneInfo("Europe/Berlin"),
        is_tracked_match=lambda match: True,
    )
    scheduler_class = sys.modules[
        plugin_class.__module__.rsplit(".", 1)[0] + ".scheduler"
    ].HLTVScheduler
    instance.scheduler.select_schedule_matches = lambda matches: (
        scheduler_class.select_schedule_matches(
            instance.scheduler,
            matches,
            datetime(2026, 10, 3, 12, tzinfo=ZoneInfo("UTC")),
        )
    )
    instance.client = SimpleNamespace()
    instance._send_presentation = AsyncMock()
    return instance


def event():
    return SimpleNamespace(
        plain_result=lambda text: ("text", text),
        chain_result=lambda chain: ("chain", chain),
        send=AsyncMock(),
    )


async def consume(handler):
    return [response async for response in handler]


def presentation(pages):
    async def iter_pages(*args, **kwargs):
        for page in pages:
            yield page

    return SimpleNamespace(iter_pages=iter_pages)


@pytest.mark.asyncio
async def test_today_renders_selected_future_day(plugin):
    past = [{"id": 1, "status": "finished", "day": "2026-10-03"}]
    future = [{"id": 2, "status": "upcoming", "day": "2026-10-04"}]
    plugin._collect_tracked_matches = AsyncMock(
        return_value={"2026-10-03": past, "2026-10-04": future}
    )
    await consume(plugin.hltv_today(event()))
    call = plugin._send_presentation.call_args
    assert call.args[1:3] == ("schedule", future)
    assert "2026-10-04" in call.args[3]


@pytest.mark.asyncio
async def test_today_keeps_live_from_previous_matchday(plugin):
    live = {"id": 3, "status": "live", "day": "2026-10-02"}
    plugin._collect_tracked_matches = AsyncMock(return_value={"2026-10-02": [live]})
    await consume(plugin.hltv_today(event()))
    assert plugin._send_presentation.call_args.args[2] == [live]


def test_schedule_uses_each_events_local_day(plugin_class):
    scheduler_class = sys.modules[
        plugin_class.__module__.rsplit(".", 1)[0] + ".scheduler"
    ].HLTVScheduler
    scheduler = scheduler_class.__new__(scheduler_class)
    scheduler.config = {"tracked_events": ["1", "2"]}
    scheduler.is_tracked_match = lambda match: True
    scheduler.get_match_timezone = lambda match: ZoneInfo(match["zone"])
    # Beijing has reached October 4 while Berlin is still October 3.
    now = datetime(2026, 10, 3, 17, tzinfo=ZoneInfo("UTC"))
    berlin = {
        "id": 1,
        "event": {"id": 1},
        "zone": "Europe/Berlin",
        "status": "upcoming",
        "starts_at": "2026-10-03T20:00:00Z",
    }
    sydney = {
        "id": 2,
        "event": {"id": 2},
        "zone": "Australia/Sydney",
        "status": "upcoming",
        "starts_at": "2026-10-04T01:00:00Z",
    }
    old_live = {
        "id": 3,
        "event": {"id": 1},
        "zone": "Europe/Berlin",
        "status": "live",
        "starts_at": "2026-10-02T21:00:00Z",
    }
    unknown_live = {
        "id": 4,
        "event": {"id": 1},
        "zone": "Europe/Berlin",
        "status": "live",
    }
    assert scheduler.select_schedule_matches(
        [berlin, sydney, old_live, unknown_live, old_live], now
    ) == [unknown_live, old_live, berlin, sydney]


@pytest.mark.asyncio
async def test_collect_keeps_live_without_start_timestamp(plugin):
    live = {"id": 3, "status": "live", "timestamp": None}
    plugin.scheduler.fetch_schedule_matches = AsyncMock(return_value=[live])
    assert await plugin._collect_tracked_matches() == {"": [live]}


@pytest.mark.asyncio
async def test_results_renders_only_most_recent_complete_day(plugin):
    recent = {"id": 2, "day": "2026-10-02"}
    plugin._collect_tracked_results = AsyncMock(
        return_value=[{"id": 1, "day": "2026-10-01"}, recent]
    )
    plugin._collect_tracked_matches = AsyncMock(return_value={})
    await consume(plugin.hltv_results(event()))
    call = plugin._send_presentation.call_args
    assert call.args[1:3] == ("results", [recent])
    assert "2026-10-02" in call.args[3]


@pytest.mark.asyncio
async def test_match_forwards_map_query_and_detail(plugin):
    detail = {"id": 12, "player_stats": [{"player_id": 1}]}
    plugin.client.find_match = AsyncMock(return_value=detail)
    await consume(plugin.hltv_match(event(), "12", " 2 "))
    call = plugin._send_presentation.call_args
    assert call.args[1:3] == ("match", [detail])
    assert call.kwargs["map_name"] == "2"


@pytest.mark.asyncio
async def test_invalid_map_stays_text(plugin):
    plugin.client.find_match = AsyncMock(return_value={"player_stats": [{}]})
    plugin.scheduler.format_match_detail = lambda *args, **kwargs: "⚠️ 地图不存在"
    responses = await consume(plugin.hltv_match(event(), "12", "99"))
    assert responses == [("text", "⚠️ 地图不存在")]
    plugin._send_presentation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("adapter rejected")])
async def test_image_send_failure_falls_back_for_that_page(plugin_class, failure):
    plugin = plugin_class.__new__(plugin_class)
    plugin.presentation = presentation(
        [
            {"image": "card.png", "caption": "summary", "text": "full stats"},
            {"image": None, "text": "page two"},
        ]
    )
    target = event()
    target.send.side_effect = [failure, None, None]
    await plugin._send_presentation(target, "match", [{}], "full stats")
    assert target.send.call_args_list[1].args == (("text", "full stats"),)
    assert target.send.call_args_list[2].args == (("text", "page two"),)


@pytest.mark.asyncio
async def test_image_success_does_not_duplicate_text(plugin_class):
    plugin = plugin_class.__new__(plugin_class)
    plugin.presentation = presentation([{"image": "card.png", "text": "full stats"}])
    target = event()
    await plugin._send_presentation(target, "match", [{}], "full stats")
    target.send.assert_awaited_once()
    assert target.send.call_args.args[0][0] == "chain"


@pytest.mark.asyncio
async def test_manual_image_timeout_does_not_immediately_duplicate(plugin_class):
    plugin = plugin_class.__new__(plugin_class)
    plugin.presentation = presentation([{"image": "card.png", "text": "full stats"}])
    target = event()
    target.send.side_effect = TimeoutError("uncertain delivery")
    await plugin._send_presentation(target, "match", [{}], "full stats")
    target.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_exists", "responses", "expected", "calls"),
    [
        (True, [None], True, 1),
        (True, [False, None], True, 2),
        (True, [TimeoutError("uncertain")], False, 1),
        (False, [None], True, 1),
    ],
)
async def test_scheduled_page_adapter_results(
    plugin_class, tmp_path, image_exists, responses, expected, calls
):
    module = importlib.import_module("_hltv_command_tests.scheduler")
    scheduler = module.HLTVScheduler.__new__(module.HLTVScheduler)
    scheduler.context = SimpleNamespace(send_message=AsyncMock(side_effect=responses))
    image_path = tmp_path / "card.png"
    if image_exists:
        image_path.write_bytes(b"test-image")
    page = {"image": str(image_path), "text": "full stats", "caption": "summary"}
    assert await scheduler._send_page("target", page) is expected
    assert scheduler.context.send_message.await_count == calls
    parts = scheduler.context.send_message.call_args.args[1].parts
    if not image_exists or calls == 2:
        assert parts == [("text", "full stats")]
    else:
        assert parts == [("image", str(image_path)), ("text", "summary")]
