"""Durable, page-level outbox. Rendering never runs in the scheduler poll loop."""

import asyncio
import copy
import json
import os
import time
from pathlib import Path

from astrbot.api import logger


class DeliveryQueue:
    def __init__(self, path, config, build, send, complete):
        self.path = Path(path)
        self.config = config
        self.build = build
        self.send = send
        self.complete = complete
        self.jobs = {}
        self._reconciled = set()
        self._task = None
        self._build_tasks = {}
        self._wake = asyncio.Event()
        if self.path.exists():
            # Fail closed: silently discarding a corrupt outbox can duplicate sends.
            self.jobs = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.jobs, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def enqueue(self, key, payload, targets):
        if key in self.jobs:
            return True
        targets = list(dict.fromkeys(targets))
        if not targets:
            return False
        self.jobs[key] = {
            "payload": copy.deepcopy(payload),
            "targets": {
                target: {"next": 0, "failures": 0, "retry_at": 0} for target in targets
            },
            "pages": None,
            "status": "pending",
            "created": time.time(),
        }
        try:
            self.save()
        except Exception:
            self.jobs.pop(key, None)
            raise
        self._wake.set()
        return True

    @property
    def status(self):
        return {
            state: sum(job["status"] == state for job in self.jobs.values())
            for state in ("pending", "failed", "completed", "cancelled")
        }

    @property
    def protected_images(self):
        return {
            page["image"]
            for job in self.jobs.values()
            if job["status"] not in {"completed", "cancelled"}
            for page in job.get("pages") or []
            if page.get("image")
        }

    async def _build_job(self, payload):
        try:
            pages = await self.build(**payload)
            if not pages:
                raise ValueError("Empty presentation")
            return pages
        except Exception:
            logger.exception("[HLTV] Rendering failed; queueing text")
            return [{"text": payload["text"], "image": None}]

    def _ordered_jobs(self):
        priority = {"reminder": 0, "report": 1, "daily": 2}
        return sorted(
            self.jobs.items(),
            key=lambda item: (
                priority.get(item[0].split(":", 1)[0], 2),
                item[1].get("created", 0),
            ),
        )

    async def process_once(self):
        max_failures = max(1, int(self.config.get("max_result_retries", 30)))
        interval = max(1, int(self.config.get("result_retry_interval", 10))) * 60
        # Build at most two frozen jobs concurrently. A long daily schedule must
        # leave the worker free to deliver an already-built reminder.
        for key, job in self._ordered_jobs():
            if len(self._build_tasks) >= 2:
                break
            if (
                job["status"] == "pending"
                and job["pages"] is None
                and key not in self._build_tasks
            ):
                task = asyncio.create_task(self._build_job(job["payload"]))
                self._build_tasks[key] = task
                task.add_done_callback(lambda _task: self._wake.set())
        # Give newly created builds a turn, without waiting for their completion.
        await asyncio.sleep(0)
        for key, job in self._ordered_jobs():
            if job["status"] in {"completed", "cancelled"}:
                # Reconcile legacy state after a crash between the two saves.
                if job["status"] == "completed" and key not in self._reconciled:
                    self.complete(key)
                    self._reconciled.add(key)
                if set(job) != {"status", "completed_at"}:
                    self.jobs[key] = {
                        "status": job["status"],
                        "completed_at": job.get("completed_at", time.time()),
                    }
                    self.save()
                continue
            if job["status"] == "failed":
                continue
            if job["pages"] is None:
                task = self._build_tasks.get(key)
                if task is None or not task.done():
                    continue
                job["pages"] = task.result()
                del self._build_tasks[key]
                self.save()
                self._wake.set()
            pages = job["pages"]
            for target, progress in job["targets"].items():
                if (
                    progress["next"] < len(pages)
                    and target not in self.config.get("notify_targets", [])
                    and not progress.get("cancelled")
                ):
                    progress["cancelled"] = True
                    self.save()
                if (
                    progress.get("cancelled")
                    or progress["failures"] >= max_failures
                    or progress["retry_at"] > time.time()
                ):
                    continue
                if progress["next"] < len(pages):
                    page = pages[progress["next"]]
                    try:
                        success = await asyncio.wait_for(
                            self.send(target, page), timeout=30
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("[HLTV] Delivery failed")
                        success = False
                    if success is False:
                        progress["failures"] += 1
                        progress["retry_at"] = time.time() + interval
                        self.save()
                    else:
                        progress["next"] += 1
                        progress["failures"] = 0
                        progress["retry_at"] = 0
                        self.save()
                        if progress["next"] < len(pages):
                            self._wake.set()
            if all(
                p["next"] == len(pages) or p.get("cancelled")
                for p in job["targets"].values()
            ):
                cancelled = any(p.get("cancelled") for p in job["targets"].values())
                # Keep only a stable task tombstone, not targets/source/media.
                self.jobs[key] = {
                    "status": "cancelled" if cancelled else "completed",
                    "completed_at": time.time(),
                }
                self.save()
                for page in pages:
                    if page.get("image"):
                        try:
                            os.utime(page["image"], None)
                        except OSError:
                            logger.warning(
                                "[HLTV] Could not update completed image retention",
                                exc_info=True,
                            )
                if not cancelled:
                    self.complete(key)
                    self._reconciled.add(key)
            elif all(
                p["next"] == len(pages)
                or p["failures"] >= max_failures
                or p.get("cancelled")
                for p in job["targets"].values()
            ):
                job["status"] = "failed"
                self.save()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                await self.process_once()
            except Exception:
                logger.exception("[HLTV] Outbox processing failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        builds = list(self._build_tasks.values())
        for task in builds:
            task.cancel()
        if builds:
            await asyncio.gather(*builds, return_exceptions=True)
        self._build_tasks.clear()
