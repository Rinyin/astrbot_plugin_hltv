import asyncio
import importlib.util
import logging
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo


def load_queue():
    module_path = Path(__file__).parents[1] / "delivery.py"
    spec = importlib.util.spec_from_file_location("hltv_delivery_test", module_path)
    module = importlib.util.module_from_spec(spec)
    # The outbox only uses the framework logger; do not require a running bot.
    previous = sys.modules.get("astrbot.api")
    if previous is None:
        sys.modules["astrbot.api"] = types.SimpleNamespace(logger=logging.getLogger())
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("astrbot.api", None)
    return module.DeliveryQueue


DeliveryQueue = load_queue()


def load_scheduler():
    package_name = "_hltv_daily_delivery_tests"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parents[1])]
    modules = {
        package_name: package,
        "astrbot.api": types.SimpleNamespace(logger=logging.getLogger()),
        "astrbot.api.event": types.SimpleNamespace(MessageChain=object),
        "astrbot.api.star": types.SimpleNamespace(Context=object, StarTools=object),
        package_name + ".api": types.SimpleNamespace(HLTVClient=object),
        package_name + ".delivery": types.SimpleNamespace(DeliveryQueue=DeliveryQueue),
    }
    spec = importlib.util.spec_from_file_location(
        package_name + ".scheduler", Path(__file__).parents[1] / "scheduler.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.HLTVScheduler


class DailyRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scheduler_class = load_scheduler()
        self.scheduler = scheduler_class.__new__(scheduler_class)
        scheduler = self.scheduler
        scheduler.config = {"daily_schedule_time": "09:00", "result_retry_interval": 10}
        scheduler.last_daily_date = None
        scheduler._daily_retry_date = None
        scheduler._daily_retry_at = 0
        scheduler.delivery = types.SimpleNamespace(jobs={})
        scheduler.client = types.SimpleNamespace(get_upcoming_matches=AsyncMock())
        scheduler.fetch_schedule_matches = scheduler.client.get_upcoming_matches
        scheduler.select_schedule_matches = lambda matches, now: matches
        scheduler.get_current_matchday = Mock(return_value="2026-10-03")
        scheduler.get_tracked_event_ids = Mock(return_value=["1"])
        scheduler.is_tracked_match = Mock(return_value=True)
        scheduler.get_matchday = Mock(return_value="2026-10-03")
        scheduler.parse_starts_at_ts = Mock(return_value=1)
        scheduler.format_daily_schedule = Mock(return_value="schedule")

        def enqueue(key, *args, **kwargs):
            scheduler.delivery.jobs[key] = {}
            return True

        scheduler._enqueue = Mock(side_effect=enqueue)

    async def check(self, minute, day=3):
        await self.scheduler.check_daily_schedule(
            datetime(2026, 10, day, 9, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
        )

    async def test_fetch_errors_and_empty_results_retry_after_configured_minute(self):
        scheduler = self.scheduler
        scheduler.client.get_upcoming_matches.side_effect = [
            RuntimeError("offline"),
            [],
            [{"id": 1}],
        ]
        with self.assertRaises(RuntimeError):
            await self.check(0)
        await self.check(1)
        self.assertEqual(scheduler.client.get_upcoming_matches.await_count, 1)
        await self.check(10)
        await self.check(11)
        self.assertEqual(scheduler.client.get_upcoming_matches.await_count, 2)
        await self.check(20)
        scheduler._enqueue.assert_called_once()
        self.assertIn("daily:2026-10-03", scheduler.delivery.jobs)
        await self.check(40)
        self.assertEqual(scheduler.client.get_upcoming_matches.await_count, 3)

    async def test_new_day_resets_retry_window(self):
        scheduler = self.scheduler
        scheduler._daily_retry_date = "2026-10-02"
        scheduler._daily_retry_at = float("inf")
        scheduler.client.get_upcoming_matches.return_value = [{"id": 1}]
        await self.check(5)
        scheduler._enqueue.assert_called_once()


class DailyPrefetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scheduler_class = load_scheduler()
        self.scheduler = scheduler_class.__new__(scheduler_class)
        scheduler = self.scheduler
        scheduler.config = {
            "daily_schedule_time": "09:00",
            "result_retry_interval": 10,
            "daily_schedule_enabled": True,
            "daily_asset_prefetch_enabled": True,
            "image_enabled": True,
            "tracked_events": ["1"],
        }
        scheduler.last_asset_prefetch_date = None
        scheduler._asset_prefetch_retry_at = 0.0
        scheduler._asset_prefetch_task = None
        scheduler.prefetched_match_starts = {}
        scheduler._match_prefetch_tasks = {}
        scheduler._match_prefetch_retry_at = {}
        scheduler._match_prefetch_starts = {}
        scheduler.last_daily_date = None
        scheduler.presentation = types.SimpleNamespace(prefetch_assets=AsyncMock())
        scheduler.get_current_matchday = Mock(return_value="2026-10-03")
        scheduler.get_tracked_event_ids = Mock(return_value=["1"])
        scheduler.get_tz = Mock(return_value=ZoneInfo("Asia/Shanghai"))
        scheduler._save_state = Mock()
        scheduler.fetch_schedule_matches = AsyncMock(return_value=[{"id": 1}])
        scheduler.select_schedule_matches = Mock(return_value=[{"id": 1}])

    @staticmethod
    def at(hour, minute):
        return datetime(2026, 10, 3, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def test_prefetch_waits_for_window_and_runs_once(self):
        scheduler = self.scheduler
        scheduler.maybe_start_asset_prefetch(self.at(7, 59))
        self.assertIsNone(scheduler._asset_prefetch_task)
        scheduler.maybe_start_asset_prefetch(self.at(8, 0))
        task = scheduler._asset_prefetch_task
        self.assertIsNotNone(task)
        await task
        await asyncio.sleep(0)
        scheduler.presentation.prefetch_assets.assert_awaited_once()
        self.assertEqual(scheduler.last_asset_prefetch_date, "2026-10-03")
        self.assertEqual(
            scheduler.select_schedule_matches.call_args.args[1],
            self.at(9, 0),
        )
        scheduler.maybe_start_asset_prefetch(self.at(8, 30))
        self.assertIsNone(scheduler._asset_prefetch_task)
        self.assertEqual(scheduler.presentation.prefetch_assets.await_count, 1)

    async def test_cross_day_window_prewarms_next_push_date(self):
        scheduler = self.scheduler
        scheduler.config["daily_schedule_time"] = "00:30"
        scheduler.maybe_start_asset_prefetch(self.at(23, 30))
        task = scheduler._asset_prefetch_task
        self.assertIsNotNone(task)
        await task
        await asyncio.sleep(0)
        # 23:30 on Oct 3 warms the Oct 4 00:30 push schedule, keyed by Oct 4.
        self.assertEqual(scheduler.last_asset_prefetch_date, "2026-10-04")
        self.assertEqual(
            scheduler.select_schedule_matches.call_args.args[1],
            datetime(2026, 10, 4, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    async def test_empty_schedule_retries_instead_of_marking_day_done(self):
        scheduler = self.scheduler
        scheduler.fetch_schedule_matches = AsyncMock(return_value=[])
        scheduler.select_schedule_matches = Mock(return_value=[])
        with self.assertLogs(level="WARNING"):
            scheduler.maybe_start_asset_prefetch(self.at(8, 0))
            task = scheduler._asset_prefetch_task
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)
        self.assertIsNone(scheduler.last_asset_prefetch_date)
        self.assertGreater(scheduler._asset_prefetch_retry_at, 0)

    async def test_prefetch_skips_after_push_or_when_disabled(self):
        scheduler = self.scheduler
        scheduler.last_daily_date = "2026-10-03"
        scheduler.maybe_start_asset_prefetch(self.at(8, 30))
        self.assertIsNone(scheduler._asset_prefetch_task)
        scheduler.last_daily_date = None
        scheduler.config["daily_asset_prefetch_enabled"] = False
        scheduler.maybe_start_asset_prefetch(self.at(8, 30))
        self.assertIsNone(scheduler._asset_prefetch_task)
        scheduler.config["daily_asset_prefetch_enabled"] = True
        scheduler.config["image_enabled"] = False
        scheduler.maybe_start_asset_prefetch(self.at(8, 30))
        self.assertIsNone(scheduler._asset_prefetch_task)

    async def test_prefetch_failure_retries_later(self):
        scheduler = self.scheduler
        scheduler.fetch_schedule_matches = AsyncMock(
            side_effect=RuntimeError("offline")
        )
        with self.assertLogs(level="WARNING"):
            scheduler.maybe_start_asset_prefetch(self.at(8, 0))
            task = scheduler._asset_prefetch_task
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)
        self.assertIsNone(scheduler.last_asset_prefetch_date)
        self.assertGreater(scheduler._asset_prefetch_retry_at, 0)

    async def test_stop_cancels_running_prefetch(self):
        scheduler = self.scheduler
        scheduler._task = None
        scheduler._running = False
        scheduler.delivery = types.SimpleNamespace(stop=AsyncMock())
        started = asyncio.Event()

        async def slow(push_date, target):
            started.set()
            await asyncio.Event().wait()

        scheduler._prefetch_daily_assets = slow
        task = asyncio.create_task(slow("2026-10-03", self.at(9, 0)))
        scheduler._asset_prefetch_task = task
        await started.wait()
        await scheduler.stop()
        self.assertTrue(task.cancelled())
        self.assertIsNone(scheduler._asset_prefetch_task)


class MatchPrefetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scheduler_class = load_scheduler()
        self.scheduler = scheduler_class.__new__(scheduler_class)
        scheduler = self.scheduler
        scheduler.config = {
            "result_retry_interval": 10,
            "match_reminder_enabled": True,
            "image_enabled": True,
            "daily_schedule_enabled": False,
            "tracked_events": ["1"],
        }
        scheduler.presentation = types.SimpleNamespace(prefetch_assets=AsyncMock())
        scheduler.prefetched_match_starts = {}
        scheduler._match_prefetch_tasks = {}
        scheduler._match_prefetch_retry_at = {}
        scheduler._match_prefetch_starts = {}
        scheduler.get_tz = Mock(return_value=ZoneInfo("Asia/Shanghai"))
        scheduler.get_tracked_event_ids = Mock(return_value=["1"])
        scheduler.get_matchday = Mock(return_value="2026-10-03")
        scheduler._save_state = Mock()

    @staticmethod
    def match(match_id="42"):
        return {
            "id": match_id,
            "event": {"id": "1", "name": "赛事"},
            "team1": {"id": "10", "name": "Alpha"},
            "team2": {"id": "20", "name": "Beta"},
        }

    async def test_window_runs_once_and_completion_dedupes(self):
        scheduler = self.scheduler
        now = 1000.0
        starts = now + 70 * 60
        scheduler.maybe_start_match_asset_prefetch(self.match(), "42", starts, now)
        task = scheduler._match_prefetch_tasks.get("42")
        self.assertIsNotNone(task)
        await task
        await asyncio.sleep(0)
        scheduler.presentation.prefetch_assets.assert_awaited_once()
        self.assertEqual(scheduler.prefetched_match_starts, {"42": starts})
        scheduler.maybe_start_match_asset_prefetch(self.match(), "42", starts, now)
        self.assertNotIn("42", scheduler._match_prefetch_tasks)
        self.assertEqual(scheduler.presentation.prefetch_assets.await_count, 1)

    async def test_outside_window_is_ignored(self):
        scheduler = self.scheduler
        now = 1000.0
        scheduler.maybe_start_match_asset_prefetch(
            self.match("1"), "1", now + 70 * 60 + 1, now
        )
        self.assertNotIn("1", scheduler._match_prefetch_tasks)
        scheduler.maybe_start_match_asset_prefetch(
            self.match("2"), "2", now + 5 * 60, now
        )
        self.assertNotIn("2", scheduler._match_prefetch_tasks)

    async def test_gated_by_reminder_and_image_but_not_daily(self):
        scheduler = self.scheduler
        now = 1000.0
        starts = now + 30 * 60
        scheduler.config["match_reminder_enabled"] = False
        scheduler.maybe_start_match_asset_prefetch(self.match("1"), "1", starts, now)
        self.assertNotIn("1", scheduler._match_prefetch_tasks)
        scheduler.config["match_reminder_enabled"] = True
        scheduler.config["image_enabled"] = False
        scheduler.maybe_start_match_asset_prefetch(self.match("2"), "2", starts, now)
        self.assertNotIn("2", scheduler._match_prefetch_tasks)
        scheduler.config["image_enabled"] = True
        scheduler.config["daily_schedule_enabled"] = False
        scheduler.maybe_start_match_asset_prefetch(self.match("3"), "3", starts, now)
        self.assertIn("3", scheduler._match_prefetch_tasks)
        await scheduler._match_prefetch_tasks["3"]

    async def test_failure_retries_after_asset_cooldown(self):
        scheduler = self.scheduler
        scheduler.presentation.prefetch_assets = AsyncMock(
            side_effect=RuntimeError("offline")
        )
        now = time.time()
        starts = now + 70 * 60
        with self.assertLogs(level="WARNING"):
            scheduler.maybe_start_match_asset_prefetch(
                self.match("9"), "9", starts, now
            )
            task = scheduler._match_prefetch_tasks["9"]
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)
        # Retry must clear the 30-minute asset cooldown, not spin at 10 minutes.
        retry_at = scheduler._match_prefetch_retry_at["9"]
        self.assertGreaterEqual(retry_at - time.time(), 30 * 60 - 5)
        # Still cooling down: no new task.
        scheduler.maybe_start_match_asset_prefetch(self.match("9"), "9", starts, now)
        self.assertNotIn("9", scheduler._match_prefetch_tasks)
        # Once the cooldown has passed and the match is still >10min away, retry.
        scheduler._match_prefetch_retry_at["9"] = time.time() - 1
        later = starts - 40 * 60
        with self.assertLogs(level="WARNING"):
            scheduler.maybe_start_match_asset_prefetch(
                self.match("9"), "9", starts, later
            )
            self.assertIn("9", scheduler._match_prefetch_tasks)
            with self.assertRaises(RuntimeError):
                await scheduler._match_prefetch_tasks["9"]
            await asyncio.sleep(0)

    async def test_reschedule_after_success_rechecks_and_persists(self):
        scheduler = self.scheduler
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scheduler.state_file_path = str(Path(temporary.name) / "state.json")
        scheduler.reminded_match_ids = set()
        scheduler.tracking_matches = {}
        scheduler.reported_match_ids = set()
        scheduler.last_daily_date = None
        scheduler.last_asset_prefetch_date = None
        scheduler.event_names = {}
        scheduler._save_state = lambda: scheduler.__class__._save_state(scheduler)

        now = 1000.0
        s1 = now + 70 * 60
        scheduler.maybe_start_match_asset_prefetch(self.match(), "42", s1, now)
        await scheduler._match_prefetch_tasks["42"]
        await asyncio.sleep(0)
        self.assertEqual(scheduler.prefetched_match_starts, {"42": s1})

        # Restart reloads the completed start so the same match is not rechecked.
        restored = scheduler.__class__.__new__(scheduler.__class__)
        restored.state_file_path = scheduler.state_file_path
        restored._load_state()
        self.assertEqual(restored.prefetched_match_starts, {"42": s1})

        # Reschedule to a different start still inside the window: fresh check.
        restored.config = dict(scheduler.config)
        restored.presentation = types.SimpleNamespace(prefetch_assets=AsyncMock())
        restored._match_prefetch_tasks = {}
        restored._match_prefetch_retry_at = {}
        restored._match_prefetch_starts = {}
        restored.get_tz = Mock(return_value=ZoneInfo("Asia/Shanghai"))
        restored.get_tracked_event_ids = Mock(return_value=["1"])
        restored.get_matchday = Mock(return_value="2026-10-03")
        restored._save_state = lambda: restored.__class__._save_state(restored)
        s2 = now + 60 * 60
        restored.maybe_start_match_asset_prefetch(self.match(), "42", s2, now)
        self.assertIn("42", restored._match_prefetch_tasks)
        await restored._match_prefetch_tasks["42"]
        await asyncio.sleep(0)
        self.assertEqual(restored.prefetched_match_starts["42"], s2)

    async def test_stale_generation_result_does_not_mark_new_start(self):
        scheduler = self.scheduler
        old_start = 1000.0 + 70 * 60
        new_start = 1000.0 + 60 * 60
        scheduler._match_prefetch_starts["42"] = new_start
        finished = asyncio.Future()
        finished.set_result(None)
        scheduler._on_match_asset_prefetch_done("42", old_start, finished)
        self.assertNotIn("42", scheduler.prefetched_match_starts)

    def test_prune_keeps_future_and_drops_old(self):
        scheduler = self.scheduler
        now = 1_000_000.0
        scheduler.prefetched_match_starts = {
            "old": now - 2 * 24 * 3600,
            "recent": now - 3600,
            "future": now + 3600,
        }
        scheduler._match_prefetch_starts = dict(scheduler.prefetched_match_starts)
        scheduler._match_prefetch_retry_at = {"old": now, "recent": now, "future": now}
        scheduler._prune_match_prefetch_state(now)
        self.assertNotIn("old", scheduler.prefetched_match_starts)
        self.assertIn("recent", scheduler.prefetched_match_starts)
        self.assertIn("future", scheduler.prefetched_match_starts)
        self.assertNotIn("old", scheduler._match_prefetch_retry_at)
        self.assertIn("future", scheduler._match_prefetch_retry_at)

    async def test_postponement_outside_window_cancels_old_task(self):
        scheduler = self.scheduler
        now = time.time()
        scheduler.maybe_start_match_asset_prefetch(self.match(), "42", now + 4200, now)
        old_task = scheduler._match_prefetch_tasks["42"]
        scheduler.maybe_start_match_asset_prefetch(self.match(), "42", now + 86400, now)
        with self.assertRaises(asyncio.CancelledError):
            await old_task
        self.assertNotIn("42", scheduler._match_prefetch_tasks)
        self.assertNotIn("42", scheduler.prefetched_match_starts)
        self.assertEqual(scheduler._match_prefetch_starts["42"], now + 86400)

    async def test_old_failure_does_not_delay_rescheduled_match(self):
        scheduler = self.scheduler
        scheduler._match_prefetch_starts["42"] = 9000.0
        old_task = asyncio.Future()
        old_task.set_exception(RuntimeError("old request failed"))
        scheduler._on_match_asset_prefetch_done("42", 8000.0, old_task)
        self.assertNotIn("42", scheduler._match_prefetch_retry_at)

    async def test_daily_prewarm_does_not_suppress_per_match_check(self):
        scheduler = self.scheduler
        scheduler.fetch_schedule_matches = AsyncMock(return_value=[self.match()])
        scheduler.select_schedule_matches = Mock(return_value=[self.match()])
        await scheduler._prefetch_daily_assets("2026-10-03", self.at(9, 0))
        # Daily success is not a per-match completion: each match still checks.
        self.assertEqual(scheduler.prefetched_match_starts, {})
        now = 1000.0
        starts = now + 60 * 60
        scheduler.maybe_start_match_asset_prefetch(self.match(), "42", starts, now)
        self.assertIn("42", scheduler._match_prefetch_tasks)
        await scheduler._match_prefetch_tasks["42"]

    @staticmethod
    def at(hour, minute):
        return datetime(2026, 10, 3, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def test_stop_cancels_match_prefetch(self):
        scheduler = self.scheduler
        scheduler._task = None
        scheduler._running = False
        scheduler.delivery = types.SimpleNamespace(stop=AsyncMock())
        scheduler._asset_prefetch_task = None
        started = asyncio.Event()

        async def slow(match, match_id, starts_ts):
            started.set()
            await asyncio.Event().wait()

        scheduler._prefetch_match_assets = slow
        task = asyncio.create_task(slow({}, "1", 0.0))
        scheduler._match_prefetch_tasks["1"] = task
        await started.wait()
        await scheduler.stop()
        self.assertTrue(task.cancelled())
        self.assertEqual(scheduler._match_prefetch_tasks, {})

    async def test_completion_persists_across_restart(self):
        scheduler = self.scheduler
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        scheduler.state_file_path = str(Path(temporary.name) / "state.json")
        scheduler.reminded_match_ids = set()
        scheduler.tracking_matches = {}
        scheduler.reported_match_ids = set()
        scheduler.last_daily_date = None
        scheduler.last_asset_prefetch_date = None
        scheduler.event_names = {}
        scheduler.prefetched_match_starts = {"99": 1234.5}
        scheduler._save_state = lambda: scheduler.__class__._save_state(scheduler)
        scheduler._save_state()
        restored = scheduler.__class__.__new__(scheduler.__class__)
        restored.state_file_path = scheduler.state_file_path
        restored._load_state()
        self.assertEqual(restored.prefetched_match_starts, {"99": 1234.5})


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "delivery.json"
        self.completed = []
        self.pages = [
            {"text": "page one", "image": None},
            {"text": "page two", "image": None},
        ]
        self.build = AsyncMock(return_value=self.pages)
        self.payload = {"kind": "daily", "matches": [{"id": 1}], "text": "fallback"}

    def queue(self, send, attempts=3):
        return DeliveryQueue(
            self.path,
            {"max_result_retries": attempts, "notify_targets": ["a", "b"]},
            self.build,
            send,
            self.completed.append,
        )

    async def test_partial_target_and_page_resume_after_restart(self):
        sent = []

        async def send(target, page):
            sent.append((target, page["text"]))
            return (target, page["text"]) != ("a", "page two")

        queue = self.queue(send)
        self.assertTrue(queue.enqueue("daily:2026-10-03", self.payload, ["a", "b"]))
        self.payload["matches"][0]["id"] = 999
        await queue.process_once()
        await queue.process_once()
        self.assertEqual(
            queue.jobs["daily:2026-10-03"]["payload"]["matches"][0]["id"], 1
        )
        self.assertEqual(self.completed, [])
        self.assertEqual(
            sent,
            [
                ("a", "page one"),
                ("b", "page one"),
                ("a", "page two"),
                ("b", "page two"),
            ],
        )
        resend = AsyncMock(return_value=True)
        restored = self.queue(resend)
        restored.jobs["daily:2026-10-03"]["targets"]["a"]["retry_at"] = 0
        await restored.process_once()
        resend.assert_awaited_once_with("a", self.pages[1])
        self.build.assert_awaited_once()
        self.assertEqual(self.completed, ["daily:2026-10-03"])
        self.assertEqual(restored.status["completed"], 1)

    async def test_all_failures_exhaust_and_remain_visible(self):
        queue = self.queue(AsyncMock(return_value=False), attempts=1)
        queue.enqueue("report:42", self.payload, ["a", "b"])
        await queue.process_once()
        self.assertEqual(queue.status["failed"], 1)
        self.assertEqual(self.completed, [])
        await queue.process_once()
        self.assertEqual(queue.send.await_count, 2)
        self.assertTrue(queue.enqueue("report:42", self.payload, ["c"]))
        self.assertEqual(set(queue.jobs["report:42"]["targets"]), {"a", "b"})

    async def test_no_targets_does_not_complete_or_create_task(self):
        queue = self.queue(AsyncMock())
        self.assertFalse(queue.enqueue("report:42", self.payload, []))
        self.assertEqual(queue.jobs, {})
        self.assertEqual(self.completed, [])

    async def test_render_failure_delivers_frozen_text(self):
        self.build.side_effect = RuntimeError("offline")
        send = AsyncMock(return_value=True)
        queue = self.queue(send)
        queue.enqueue("report:42", self.payload, ["a"])
        with self.assertLogs(level="ERROR"):
            await queue.process_once()
        send.assert_awaited_once_with("a", {"text": "fallback", "image": None})
        self.assertEqual(self.completed, ["report:42"])

    async def test_enqueue_does_not_wait_for_renderer(self):
        queue = self.queue(AsyncMock())
        queue.enqueue("report:42", self.payload, ["a"])
        self.build.assert_not_awaited()
        self.assertTrue(self.path.exists())

    async def test_completed_tombstone_reconciles_after_restart(self):
        queue = self.queue(AsyncMock(return_value=True))
        queue.enqueue("report:42", self.payload, ["a"])
        await queue.process_once()
        await queue.process_once()
        self.completed.clear()
        restored = self.queue(AsyncMock())
        await restored.process_once()
        self.assertEqual(self.completed, ["report:42"])
        restored.send.assert_not_awaited()
        self.assertEqual(set(restored.jobs["report:42"]), {"status", "completed_at"})
        restored.save = Mock(wraps=restored.save)
        await restored.process_once()
        await restored.process_once()
        restored.save.assert_not_called()
        self.assertEqual(self.completed, ["report:42"])

    async def test_long_schedule_build_does_not_block_reminder(self):
        blocked = asyncio.Event()
        started = asyncio.Event()

        async def build(**payload):
            if payload["kind"] == "daily":
                started.set()
                await blocked.wait()
            return [{"text": payload["kind"], "image": None}]

        send = AsyncMock(return_value=True)
        queue = self.queue(send)
        queue.build = AsyncMock(side_effect=build)
        queue.enqueue("daily:2026-10-03", self.payload, ["a"])
        await asyncio.wait_for(queue.process_once(), 0.5)
        self.assertTrue(started.is_set())
        send.assert_not_awaited()
        queue.enqueue("reminder:42", {**self.payload, "kind": "reminder"}, ["a"])
        await asyncio.wait_for(queue.process_once(), 0.5)
        send.assert_awaited_once_with("a", {"text": "reminder", "image": None})
        self.assertEqual(queue.jobs["reminder:42"]["status"], "completed")
        self.assertEqual(queue.jobs["daily:2026-10-03"]["status"], "pending")
        await queue.process_once()
        self.assertEqual(queue.build.await_count, 2)
        await queue.stop()
        self.assertEqual(queue._build_tasks, {})
        restored = self.queue(AsyncMock(return_value=True))
        self.assertIsNone(restored.jobs["daily:2026-10-03"]["pages"])

    async def test_unsubscribe_cancels_remaining_pages_without_completion(self):
        send = AsyncMock(return_value=True)
        queue = self.queue(send)
        queue.enqueue("report:42", self.payload, ["a"])
        await queue.process_once()
        send.assert_awaited_once_with("a", self.pages[0])
        queue.config["notify_targets"] = ["b"]
        await queue.process_once()
        self.assertEqual(send.await_count, 1)
        self.assertEqual(queue.status["cancelled"], 1)
        self.assertEqual(self.completed, [])
        self.assertTrue(queue.enqueue("report:42", self.payload, ["b"]))
        await queue.process_once()
        self.assertEqual(send.await_count, 1)
        self.assertEqual(self.completed, [])


if __name__ == "__main__":
    unittest.main()
