"""Small in-process stand-ins for production infrastructure.

  TTLCache      -> Redis (shortlist cache)
  RateLimiter   -> Redis or the API gateway
  run_job       -> SQS queue + worker
They keep the same interfaces so swapping them out does not touch the endpoints.
"""

import json
import logging
import threading
import time
from collections import defaultdict, deque

from app import classifier
from app.db import connect
from app.security import decrypt

log = logging.getLogger("matchcheck.infra")


class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._data: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple):
        with self._lock:
            hit = self._data.get(key)
            if hit is None or hit[0] < time.monotonic():
                self._data.pop(key, None)
                return None
            return hit[1]

    def set(self, key: tuple, value) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)

    def invalidate_prefix(self, prefix: tuple) -> None:
        with self._lock:
            for key in [k for k in self._data if k[: len(prefix)] == prefix]:
                del self._data[key]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RateLimiter:
    """Sliding window limiter keyed by user id or IP address."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False, int(window_seconds - (now - q[0])) + 1
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def classify_rejection_job(rejection_id: str, tenant_id: str) -> None:
    """Worker: classify one queued rejection. Runs outside the request path."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT r.raw_text_enc, s.client_id, c.name AS client_name, k.name AS candidate_name, p.hard_json
               FROM rejections r
               JOIN shares s ON s.id = r.share_id AND s.tenant_id = r.tenant_id
               JOIN clients c ON c.id = s.client_id
               JOIN candidates k ON k.id = s.candidate_id
               JOIN preference_cards p ON p.client_id = s.client_id
               WHERE r.id = ? AND r.tenant_id = ?""",
            (rejection_id, tenant_id),
        ).fetchone()
        if row is None:
            return
        try:
            result = classifier.classify(
                decrypt(row["raw_text_enc"]),
                json.loads(row["hard_json"]),
                names=[row["client_name"], row["candidate_name"]],
            )
            conn.execute(
                """UPDATE rejections SET status = 'done', reason_code = ?, reason_detail = ?, confidence = ?,
                          classifier = ?, suggestion_json = ? WHERE id = ? AND tenant_id = ?""",
                (result["reason_code"], result["reason_detail"], result["confidence"], result["classifier"],
                 json.dumps(result["suggestion"]) if result["suggestion"] else None, rejection_id, tenant_id),
            )
        except Exception:
            log.exception("classification failed for %s", rejection_id)
            conn.execute("UPDATE rejections SET status = 'failed' WHERE id = ?", (rejection_id,))
        conn.commit()
    finally:
        conn.close()
