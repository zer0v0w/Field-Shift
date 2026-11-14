"""File-backed cache for HTTP responses and extracted observations.

The cache is deliberately simple and inspectable: one payload file plus a JSON
metadata sidecar per entry.  Keys are hashes of a namespace and a caller key,
so long URLs and query strings do not become filenames.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .base import utc_now_iso


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Cache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _base(self, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(f"{namespace}\n{key}".encode("utf-8")).hexdigest()
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / digest

    def _meta_path(self, base: Path) -> Path:
        return base.with_suffix(base.suffix + ".meta.json")

    def _read_meta(self, base: Path) -> dict[str, Any]:
        meta_path = self._meta_path(base)
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _is_fresh(self, base: Path, max_age_s: float | None) -> bool:
        if max_age_s is None:
            return True
        meta = self._read_meta(base)
        fetched = meta.get("fetched_at")
        if not fetched:
            return False
        try:
            then = datetime.fromisoformat(fetched)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - then).total_seconds()
            return age <= max_age_s
        except Exception:
            return False

    def get_bytes(
        self, namespace: str, key: str, *, suffix: str = ".bin", max_age_s: float | None = None
    ) -> bytes | None:
        base = self._base(namespace, key)
        payload_path = base.with_suffix(base.suffix + suffix)
        if not payload_path.exists() or not self._is_fresh(base, max_age_s):
            return None
        return payload_path.read_bytes()

    def set_bytes(
        self,
        namespace: str,
        key: str,
        payload: bytes,
        *,
        suffix: str = ".bin",
        url: str | None = None,
    ) -> Path:
        base = self._base(namespace, key)
        payload_path = base.with_suffix(base.suffix + suffix)
        tmp = payload_path.with_suffix(payload_path.suffix + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, payload_path)
        meta = {
            "namespace": namespace,
            "key": key,
            "url": url,
            "fetched_at": utc_now_iso(),
            "sha256": sha256_bytes(payload),
            "size": len(payload),
        }
        self._meta_path(base).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return payload_path

    def get_json(
        self, namespace: str, key: str, *, max_age_s: float | None = None
    ) -> dict[str, Any] | None:
        raw = self.get_bytes(namespace, key, suffix=".json", max_age_s=max_age_s)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set_json(self, namespace: str, key: str, payload: dict[str, Any]) -> Path:
        return self.set_bytes(
            namespace,
            key,
            json.dumps(payload, indent=2, default=str).encode("utf-8"),
            suffix=".json",
        )

    def fetch_url(
        self,
        session: requests.Session,
        url: str,
        *,
        namespace: str,
        key: str | None = None,
        suffix: str = ".bin",
        timeout: float = 60,
        max_age_s: float | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    ) -> tuple[bytes, bool]:
        """Return ``(payload, was_cached)``.

        A cached payload is returned only when it is fresh under ``max_age_s``.
        Set ``max_age_s=None`` for immutable assets (COGs) and an explicit age
        for mutable API responses.  ``params`` is forwarded to the request so
        callers can keep query strings out of cache keys.
        """
        key = key or url
        cached = self.get_bytes(namespace, key, suffix=suffix, max_age_s=max_age_s)
        if cached is not None:
            return cached, True

        lock = self._lock(f"{namespace}:{key}")
        with lock:
            # Another thread may have populated the entry while we waited.
            cached = self.get_bytes(namespace, key, suffix=suffix, max_age_s=max_age_s)
            if cached is not None:
                return cached, True
            response = session.get(url, timeout=timeout, headers=headers, params=params)
            response.raise_for_status()
            payload = response.content
            self.set_bytes(namespace, key, payload, suffix=suffix, url=response.url)
            return payload, False

    def fetch_json(
        self,
        session: requests.Session,
        url: str,
        *,
        namespace: str,
        key: str | None = None,
        timeout: float = 60,
        max_age_s: float | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload, cached = self.fetch_url(
            session,
            url,
            namespace=namespace,
            key=key,
            suffix=".json",
            timeout=timeout,
            max_age_s=max_age_s,
            headers=headers,
            params=params,
        )
        return json.loads(payload), cached

    def download_file(
        self,
        session: requests.Session,
        url: str,
        destination: Path,
        *,
        timeout: float = 120,
        headers: dict[str, str] | None = None,
    ) -> Path:
        """Download a large file atomically, reusing an existing file."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_size > 0:
            return destination
        tmp = destination.with_suffix(destination.suffix + ".part")
        with session.get(url, timeout=timeout, headers=headers, stream=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        os.replace(tmp, destination)
        return destination
