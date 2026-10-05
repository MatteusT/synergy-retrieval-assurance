"""HTTP client for the running Vestigare stack.

Talks only to the public API a reviewer uses. Base URL defaults to the API
port on this machine (or an SSH tunnel to the server). No database, no
worker imports.
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests

DEFAULT_BASE = os.environ.get("VESTIGARE_API_BASE", "http://127.0.0.1:8000/api")
QUEUE_DEPTH_LIMIT = 20000


class StackError(RuntimeError):
    pass


class Stack:
    def __init__(self, base: str | None = None, timeout: float = 120.0) -> None:
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method: str, path: str, *, expected: tuple[int, ...] = (200,), **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        kwargs.setdefault("timeout", self.timeout)
        last: Exception | None = None
        for attempt in range(6):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last = exc
                sleep = min(30, 2 ** attempt)
                print(f"  {method} {path} failed ({exc.__class__.__name__}); retry in {sleep}s", flush=True)
                time.sleep(sleep)
                continue
            if response.status_code not in expected:
                if response.status_code >= 500 and attempt < 5:
                    sleep = min(30, 2 ** attempt)
                    print(f"  {method} {path} -> {response.status_code}; retry in {sleep}s", flush=True)
                    time.sleep(sleep)
                    continue
                body = (response.text or "")[:800]
                raise StackError(f"{method} {url} -> {response.status_code}: {body}")
            return response
        raise StackError(f"{method} {url} failed after retries: {last}")

    def json(self, method: str, path: str, *, expected: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        return self.request(method, path, expected=expected, **kwargs).json()

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.json("GET", path, **kwargs)

    def post(self, path: str, payload: dict | None = None, *, expected: tuple[int, ...] = (200,), **kwargs: Any) -> Any:
        return self.json("POST", path, expected=expected, json=payload or {}, **kwargs)

    def queue_depth(self, case_id: str) -> int:
        summary = self.get(f"/cases/{case_id}/progress/summary")
        return int(summary.get("global_queue_depth") or summary.get("ingest_queue_depth") or 0)

    def wait_for_queue(self, case_id: str, incoming: int, *, max_sleep: float = 300.0) -> None:
        """Back off while the ingest queues plus this batch would pass the guard."""
        sleep = 5.0
        while True:
            depth = self.queue_depth(case_id)
            if depth + incoming <= QUEUE_DEPTH_LIMIT:
                return
            print(f"  queue depth {depth} + {incoming} exceeds {QUEUE_DEPTH_LIMIT}; sleeping {sleep:.0f}s", flush=True)
            time.sleep(sleep)
            sleep = min(sleep * 2, max_sleep)
