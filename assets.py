"""Persistent, stale-while-revalidate cache for public HLTV image assets."""

import asyncio
import base64
import hashlib
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from PIL import Image

from astrbot.api import logger


def image_mime(data: bytes) -> str:
    """Validate the actual content, including a deliberately inert SVG subset."""
    if data.lstrip().startswith((b"<svg", b"<?xml")):
        root = ET.fromstring(data)
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise ValueError("Not an SVG")
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] in {
                "script",
                "foreignObject",
                "image",
            }:
                raise ValueError("Active SVG content")
            for key, value in node.attrib.items():
                if (
                    key.lower().startswith("on")
                    or ("href" in key and not value.startswith("#"))
                    or any(
                        not ref.strip(" \"'").startswith("#")
                        for ref in re.findall(r"url\(([^)]*)\)", value, re.I)
                    )
                ):
                    raise ValueError("External SVG content")
            if node.text and (
                "@import" in node.text.lower()
                or any(
                    not ref.strip(" \"'").startswith("#")
                    for ref in re.findall(r"url\(([^)]*)\)", node.text, re.I)
                )
            ):
                raise ValueError("External SVG stylesheet")
        return "image/svg+xml"
    with Image.open(io.BytesIO(data)) as img:
        if img.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("Unsupported image format")
        if img.width * img.height > 25_000_000:
            raise ValueError("Image dimensions exceed limit")
        mime = Image.MIME[img.format]
        img.verify()
        return mime


class AssetCache:
    def __init__(self, directory, refresh_days=90, max_mb=1024, download_asset=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.refresh_seconds = max(1, int(refresh_days)) * 86400
        self.max_bytes = max(1, int(max_mb)) * 1024 * 1024
        self.index_path = self.directory / "index.json"
        self.index = {}
        if self.index_path.exists():
            try:
                self.index = json.loads(self.index_path.read_text("utf-8"))
            except (ValueError, OSError):
                logger.warning("[HLTV] 素材索引损坏，将重新缓存", exc_info=True)
        self._session = None
        self._tasks = {}
        self._semaphore = asyncio.Semaphore(4)
        self._download_asset = download_asset
        self._closing = False

    def _local_override(self, kind, entity_id):
        """Stable user-owned files take precedence and are never cache-pruned."""
        if kind not in {"team", "player", "event"} or not re.fullmatch(
            r"[A-Za-z0-9_-]+", str(entity_id or "")
        ):
            return None
        for extension in ("png", "jpg", "jpeg", "webp", "svg"):
            path = self.directory / "local" / kind / f"{entity_id}.{extension}"
            if not path.is_file():
                continue
            try:
                # Bound the read itself, including files changed during the read.
                with path.open("rb") as source:
                    content = source.read(5 * 1024 * 1024 + 1)
                if len(content) > 5 * 1024 * 1024:
                    raise ValueError("Local asset exceeds 5MB")
                mime = image_mime(content)
                return f"data:{mime};base64,{base64.b64encode(content).decode()}"
            except (OSError, ValueError, ET.ParseError):
                logger.warning("[HLTV] 本地素材无效: %s", path.name, exc_info=True)
        return None

    def _save(self):
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.index, ensure_ascii=False), "utf-8")
        temporary.replace(self.index_path)

    def _data_uri(self, entry):
        if not entry.get("file"):
            return None
        path = self.directory / Path(entry["file"]).name
        try:
            content = path.read_bytes()
            mime = image_mime(content)
            return f"data:{mime};base64,{base64.b64encode(content).decode()}"
        except (OSError, ValueError, ET.ParseError):
            logger.warning("[HLTV] 缓存素材不可读，将重新获取", exc_info=True)
            return None

    async def _cached(self, key):
        # Decode a snapshot off the event loop; only mutate the live index here.
        # A concurrent refresh can replace the entry while the file is read.
        while not self._closing:
            entry = self.index.get(key, {})
            snapshot = dict(entry)
            result = await asyncio.to_thread(self._data_uri, snapshot)
            current = self.index.get(key, {})
            if current is not entry or current.get("file") != snapshot.get("file"):
                if key not in self.index:
                    return None
                continue
            if result is None:
                current.pop("file", None)
            return result
        return None

    async def get(self, kind, entity_id, url):
        if self._closing:
            return None
        local = await asyncio.to_thread(self._local_override, kind, entity_id)
        if self._closing:
            return None
        if local:
            return local
        if not url or urlparse(str(url)).scheme not in {"http", "https"}:
            if entity_id:
                entry = self.index.get(f"{kind}:{entity_id}", {})
                entry["accessed"] = time.time()
                return await self._cached(f"{kind}:{entity_id}")
            return None
        url = str(url)
        key = f"{kind}:{entity_id or hashlib.sha256(url.encode()).hexdigest()}"
        now = time.time()
        entry = self.index.setdefault(key, {})
        entry["accessed"] = now
        cached = await self._cached(key)
        if self._closing:
            return None
        entry = self.index[key]
        fresh = (
            entry.get("url") == url
            and now - entry.get("checked", 0) < self.refresh_seconds
        )
        cooling = entry.get("failed_url") == url and entry.get("retry_at", 0) > now
        if (fresh and cached) or cooling:
            return cached
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._refresh(key, url))
            self._tasks[key] = task
            task.add_done_callback(lambda done, k=key: self._tasks.pop(k, None))
        if cached:
            return cached
        # Shield the download so a page's five-second asset budget does not cancel it.
        await asyncio.shield(task)
        return await self._cached(key)

    async def _download(self, url, headers):
        if self._closing:
            raise RuntimeError("Asset cache is closing")
        if self._download_asset is not None:
            try:
                return await self._download_asset(url, headers=headers)
            except aiohttp.ClientResponseError as error:
                if error.status != 404:
                    raise
                # An older API has no asset route. Its validators do not belong
                # to the origin server, so request a complete original image.
                headers = {"User-Agent": "AstrBot-HLTV/1.0"}
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        async with self._session.get(url, headers=headers) as response:
            metadata = {
                "ETag": response.headers.get("ETag"),
                "Last-Modified": response.headers.get("Last-Modified"),
            }
            if response.status == 304:
                return None, metadata
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > 5 * 1024 * 1024:
                    raise ValueError("Asset exceeds 5MB")
            return bytes(data), metadata

    async def _refresh(self, key, url):
        try:
            async with self._semaphore:
                if self._closing:
                    return
                old = self.index[key]
                headers = {"User-Agent": "AstrBot-HLTV/1.0"}
                if old.get("url") == url and old.get("file"):
                    if old.get("etag"):
                        headers["If-None-Match"] = old["etag"]
                    if old.get("modified"):
                        headers["If-Modified-Since"] = old["modified"]
                data, metadata = await self._download(url, headers)
                if data is None:
                    if not old.get("file"):
                        raise ValueError("Asset returned 304 without a cached file")
                    old["checked"] = time.time()
                    old.pop("retry_at", None)
                    self._save()
                    return
                if len(data) > 5 * 1024 * 1024:
                    raise ValueError("Asset exceeds 5MB")
                await asyncio.to_thread(image_mime, data)
                name = hashlib.sha256((key + url).encode()).hexdigest() + ".image"
                path = self.directory / name
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.replace(path)
                self.index[key] = {
                    "file": name,
                    "url": url,
                    "checked": time.time(),
                    "accessed": old.get("accessed", time.time()),
                    "etag": metadata.get("ETag"),
                    "modified": metadata.get("Last-Modified"),
                }
                self.prune(protected={name})
                self._save()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[HLTV] 素材下载失败，保留旧图或使用占位图", exc_info=True)
            self.index[key].update(failed_url=url, retry_at=time.time() + 1800)
            self._save()

    def prune(self, protected=None):
        protected = protected or set()
        files = list(self.directory.glob("*.image"))
        total = sum(p.stat().st_size for p in files)
        if total <= self.max_bytes:
            return
        current = {v.get("file"): v for v in self.index.values()}
        files.sort(key=lambda p: current.get(p.name, {}).get("accessed", 0))
        for path in files:
            if total <= self.max_bytes:
                break
            if path.name in protected:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
            if path.name in current:
                current[path.name].pop("file", None)

    async def close(self):
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self._save()
        finally:
            if self._session:
                await self._session.close()
