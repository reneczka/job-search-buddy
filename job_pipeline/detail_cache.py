from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _cache_path() -> Path:
    raw = (os.getenv("JOB_PIPELINE_DETAIL_CACHE_PATH") or ".cache/job_pipeline/detail_cache.json").strip()
    return Path(raw).expanduser()


class DetailCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, Any]] | None = None

    def get(self, key: str, content_signature: str) -> dict[str, Any] | None:
        entry = self._load().get(key)
        if not isinstance(entry, dict):
            return None
        if str(entry.get("content_signature") or "") != content_signature:
            return None
        payload = entry.get("payload")
        return payload if isinstance(payload, dict) else None

    def set(self, key: str, content_signature: str, payload: dict[str, Any]) -> None:
        entries = self._load()
        entries[key] = {
            "content_signature": content_signature,
            "payload": payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        if not self.path.exists():
            self._entries = {}
            return self._entries
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._entries = {}
            return self._entries
        entries = payload.get("entries") if isinstance(payload, dict) else {}
        self._entries = entries if isinstance(entries, dict) else {}
        return self._entries


_DETAIL_CACHE = DetailCache(_cache_path())


def get_detail_cache() -> DetailCache:
    return _DETAIL_CACHE
