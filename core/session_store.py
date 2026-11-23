"""
session_store.py — Per-conversation session state and history, kept in Redis.

Layout (prefix defaults to "ilubot:sess:"):
    <prefix><sid>           HASH  created_at, last_active, voice, ...
    <prefix><sid>:history   LIST  JSON messages {"role", "text", "ts", "hidden"}

Both keys share one sliding TTL (SESSION_TTL_SECONDS, default 1800 = 30 min)
that is reset on every touch, so Redis itself deletes a session once it has been
inactive that long; no sweeper job is needed.

If Redis is unreachable at startup, an in-process store with the same API and
the same expiry rules is used instead, with a loud warning. That keeps local
development working, but history is then lost on restart and not shared across
instances, so production must set REDIS_URL.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("SessionStore")

SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "1800"))
# Hard cap on stored messages per session, so one long session can't grow unbounded.
MAX_STORED_MESSAGES = int(os.environ.get("SESSION_MAX_MESSAGES", "200"))
KEY_PREFIX = os.environ.get("SESSION_KEY_PREFIX", "ilubot:sess:")


class RedisSessionStore:
    backend = "redis"

    def __init__(self, url: str, ttl: int = SESSION_TTL_SECONDS):
        import redis  # imported lazily so the module loads without the package
        self.r = redis.Redis.from_url(url, decode_responses=True,
                                      socket_connect_timeout=3, socket_timeout=5)
        self.r.ping()
        self.ttl = ttl

    def _meta(self, sid: str) -> str:
        return f"{KEY_PREFIX}{sid}"

    def _hist(self, sid: str) -> str:
        return f"{KEY_PREFIX}{sid}:history"

    def exists(self, sid: str) -> bool:
        return bool(self.r.exists(self._meta(sid)))

    def touch(self, sid: str) -> bool:
        """Create the session if needed and reset its inactivity timer.
        Returns True if the session already existed (i.e. had not expired)."""
        now = time.time()
        meta = self._meta(sid)
        p = self.r.pipeline()
        p.hsetnx(meta, "created_at", now)
        p.hset(meta, "last_active", now)
        p.expire(meta, self.ttl)
        p.expire(self._hist(sid), self.ttl)
        created, *_ = p.execute()
        return not created

    def get_meta(self, sid: str) -> dict[str, str]:
        return self.r.hgetall(self._meta(sid))

    def set_meta(self, sid: str, **fields: Any) -> None:
        p = self.r.pipeline()
        p.hset(self._meta(sid), mapping={k: str(v) for k, v in fields.items()})
        p.expire(self._meta(sid), self.ttl)
        p.execute()

    def append(self, sid: str, role: str, text: str, hidden: bool = False) -> None:
        msg = json.dumps({"role": role, "text": text, "ts": time.time(), "hidden": hidden})
        hist = self._hist(sid)
        p = self.r.pipeline()
        p.rpush(hist, msg)
        p.ltrim(hist, -MAX_STORED_MESSAGES, -1)
        p.expire(hist, self.ttl)
        p.hset(self._meta(sid), "last_active", time.time())
        p.expire(self._meta(sid), self.ttl)
        p.execute()

    def history(self, sid: str, limit: int | None = None) -> list[dict]:
        start = -limit if limit else 0
        return [json.loads(x) for x in self.r.lrange(self._hist(sid), start, -1)]

    def delete(self, sid: str) -> None:
        self.r.delete(self._meta(sid), self._hist(sid))


class MemorySessionStore:
    """Same API and expiry semantics as RedisSessionStore, in process memory."""
    backend = "memory"

    def __init__(self, ttl: int = SESSION_TTL_SECONDS):
        self.ttl = ttl
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _live(self, sid: str) -> dict | None:
        s = self._data.get(sid)
        if s and time.time() - s["last_active"] > self.ttl:
            del self._data[sid]
            return None
        return s

    def _sweep(self) -> None:
        cutoff = time.time() - self.ttl
        for sid in [k for k, v in self._data.items() if v["last_active"] < cutoff]:
            del self._data[sid]

    def exists(self, sid: str) -> bool:
        with self._lock:
            return self._live(sid) is not None

    def touch(self, sid: str) -> bool:
        with self._lock:
            self._sweep()
            now = time.time()
            s = self._live(sid)
            if s:
                s["last_active"] = now
                return True
            self._data[sid] = {"meta": {"created_at": str(now)}, "history": [], "last_active": now}
            return False

    def get_meta(self, sid: str) -> dict[str, str]:
        with self._lock:
            s = self._live(sid)
            return dict(s["meta"]) if s else {}

    def set_meta(self, sid: str, **fields: Any) -> None:
        with self._lock:
            s = self._live(sid)
            if s:
                s["meta"].update({k: str(v) for k, v in fields.items()})
                s["last_active"] = time.time()

    def append(self, sid: str, role: str, text: str, hidden: bool = False) -> None:
        with self._lock:
            s = self._live(sid)
            if not s:
                return
            s["history"].append({"role": role, "text": text, "ts": time.time(), "hidden": hidden})
            del s["history"][:-MAX_STORED_MESSAGES]
            s["last_active"] = time.time()

    def history(self, sid: str, limit: int | None = None) -> list[dict]:
        with self._lock:
            s = self._live(sid)
            h = list(s["history"]) if s else []
            return h[-limit:] if limit else h

    def delete(self, sid: str) -> None:
        with self._lock:
            self._data.pop(sid, None)


def create_session_store():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0").strip()
    try:
        store = RedisSessionStore(url)
        logger.info(f"Session store: Redis ({url.split('@')[-1]}), inactivity TTL {SESSION_TTL_SECONDS}s")
        return store
    except Exception as e:
        logger.warning(
            f"Redis unavailable ({e}). Falling back to IN-MEMORY sessions: history is lost "
            f"on restart and not shared across instances. Set REDIS_URL for production."
        )
        return MemorySessionStore()
