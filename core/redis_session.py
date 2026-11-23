"""
core/redis_session.py — Redis-backed user session and conversation history manager for Ilubot.

Provides per-user session isolation and conversation history persistence with an
automatic 30-minute (1800s) TTL. If 30 minutes pass without activity, the session
and its history are automatically cleaned by Redis.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("RedisSession")

DEFAULT_SESSION_TTL = int(os.environ.get("REDIS_SESSION_TTL", "1800"))  # 30 minutes (in seconds)
REDIS_KEY_PREFIX = "ilubot:session:"


class _InMemoryFallback:
    """In-memory session store fallback when Redis is unavailable, respecting 30m TTL."""

    def __init__(self, default_ttl: int = DEFAULT_SESSION_TTL):
        self.default_ttl = default_ttl
        self._store: dict[str, str] = {}
        self._expires: dict[str, float] = {}

    def _purge_expired(self) -> None:
        now = time.time()
        expired_keys = [k for k, exp in self._expires.items() if exp < now]
        for k in expired_keys:
            self._store.pop(k, None)
            self._expires.pop(k, None)

    def get(self, key: str) -> Optional[str]:
        self._purge_expired()
        now = time.time()
        if key in self._expires and self._expires[key] < now:
            self._store.pop(key, None)
            self._expires.pop(key, None)
            return None
        return self._store.get(key)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._purge_expired()
        self._store[key] = value
        self._expires[key] = time.time() + ttl

    def expire(self, key: str, ttl: int) -> bool:
        self._purge_expired()
        if key in self._store:
            self._expires[key] = time.time() + ttl
            return True
        return False

    def ttl(self, key: str) -> int:
        self._purge_expired()
        if key not in self._store or key not in self._expires:
            return -2
        rem = int(self._expires[key] - time.time())
        return max(0, rem)

    def delete(self, key: str) -> bool:
        existed = key in self._store
        self._store.pop(key, None)
        self._expires.pop(key, None)
        return existed


class RedisSessionManager:
    """Manages user sessions and chat history in Redis with 30-minute TTL."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        ttl_seconds: int = DEFAULT_SESSION_TTL,
    ):
        self.ttl_seconds = ttl_seconds
        self.redis_url = redis_url or os.environ.get("REDIS_URL")
        self.client: Any = None
        self.is_redis_available = False
        self._fallback = _InMemoryFallback(default_ttl=self.ttl_seconds)

        self._init_connection()

    def _init_connection(self) -> None:
        """Attempt to connect to Redis; fall back to in-memory store on failure."""
        try:
            import redis

            if self.redis_url:
                logger.info(f"Connecting to Redis at {self.redis_url}...")
                self.client = redis.Redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2.5,
                    socket_timeout=2.5,
                )
            else:
                host = os.environ.get("REDIS_HOST", "redis" if os.path.exists("/.dockerenv") else "localhost")
                port = int(os.environ.get("REDIS_PORT", "6379"))
                password = os.environ.get("REDIS_PASSWORD") or None
                db = int(os.environ.get("REDIS_DB", "0"))
                logger.info(f"Connecting to Redis at {host}:{port} (db={db})...")
                self.client = redis.Redis(
                    host=host,
                    port=port,
                    db=db,
                    password=password,
                    decode_responses=True,
                    socket_connect_timeout=2.5,
                    socket_timeout=2.5,
                )

            # Test connection
            self.client.ping()
            self.is_redis_available = True
            logger.info(f"✅ Connected to Redis successfully. Session TTL set to {self.ttl_seconds}s (30 min).")
        except Exception as e:
            self.is_redis_available = False
            self.client = None
            logger.warning(
                f"⚠️ Redis connection unavailable ({e}). Using in-memory session cache with {self.ttl_seconds}s (30m) TTL."
            )

    def _key(self, session_id: str) -> str:
        return f"{REDIS_KEY_PREFIX}{session_id}"

    def _get_raw(self, key: str) -> Optional[str]:
        if self.is_redis_available and self.client:
            try:
                return self.client.get(key)
            except Exception as e:
                logger.error(f"Redis get error: {e}")
                self._init_connection()
        return self._fallback.get(key)

    def _setex_raw(self, key: str, ttl: int, value: str) -> None:
        if self.is_redis_available and self.client:
            try:
                self.client.setex(key, ttl, value)
                return
            except Exception as e:
                logger.error(f"Redis setex error: {e}")
                self._init_connection()
        self._fallback.setex(key, ttl, value)

    def _expire_raw(self, key: str, ttl: int) -> bool:
        if self.is_redis_available and self.client:
            try:
                return bool(self.client.expire(key, ttl))
            except Exception as e:
                logger.error(f"Redis expire error: {e}")
        return self._fallback.expire(key, ttl)

    def _delete_raw(self, key: str) -> bool:
        if self.is_redis_available and self.client:
            try:
                return bool(self.client.delete(key))
            except Exception as e:
                logger.error(f"Redis delete error: {e}")
        return self._fallback.delete(key)

    def _ttl_raw(self, key: str) -> int:
        if self.is_redis_available and self.client:
            try:
                return int(self.client.ttl(key))
            except Exception as e:
                logger.error(f"Redis ttl error: {e}")
        return self._fallback.ttl(key)

    # ── High-level Session API ────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Retrieve existing session from Redis. Refreshes the 30-minute TTL."""
        if not session_id:
            return None
        key = self._key(session_id)
        raw = self._get_raw(key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            # Refresh 30m TTL on read/activity
            self._expire_raw(key, self.ttl_seconds)
            return data
        except Exception as e:
            logger.error(f"Error parsing session JSON for {session_id}: {e}")
            return None

    def create_or_get_session(
        self,
        session_id: str,
        teaching_language: str = "English",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Fetch existing session or initialize a fresh one, setting 30-min TTL."""
        session = self.get_session(session_id)
        if session is not None:
            return session

        now = time.time()
        session = {
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "teaching_language": teaching_language,
            "metadata": metadata or {},
            "messages": [],
        }
        key = self._key(session_id)
        self._setex_raw(key, self.ttl_seconds, json.dumps(session))
        logger.info(f"Created new session {session_id} (TTL: {self.ttl_seconds}s).")
        return session

    def add_message(
        self,
        session_id: str,
        role: str,
        text: str,
        name: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Append a message to the session history in Redis and refresh 30m TTL."""
        session = self.create_or_get_session(session_id)
        now = time.time()
        msg = {
            "role": role,  # 'user' or 'model' / 'assistant'
            "text": text,
            "name": name or ("You" if role == "user" else "Ilubot"),
            "timestamp": now,
        }
        if metadata:
            msg["metadata"] = metadata

        session.setdefault("messages", []).append(msg)
        session["updated_at"] = now

        key = self._key(session_id)
        self._setex_raw(key, self.ttl_seconds, json.dumps(session))
        return msg

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return the conversation message history for a session."""
        session = self.get_session(session_id)
        if not session:
            return []
        return session.get("messages", [])

    def get_gemini_history(self, session_id: str) -> list[Any]:
        """Transform stored session messages into google.genai Content objects for chat restoration."""
        history_msgs = self.get_history(session_id)
        if not history_msgs:
            return []

        try:
            from google.genai import types

            contents = []
            for m in history_msgs:
                role = "user" if m.get("role") == "user" else "model"
                text = (m.get("text") or "").strip()
                if text:
                    contents.append(
                        types.Content(
                            role=role,
                            parts=[types.Part.from_text(text=text)],
                        )
                    )
            return contents
        except Exception as e:
            logger.error(f"Error converting history to Gemini Content: {e}")
            return []

    def set_session_language(self, session_id: str, language: str) -> None:
        """Update teaching language for a session and refresh 30m TTL."""
        session = self.create_or_get_session(session_id)
        session["teaching_language"] = language
        session["updated_at"] = time.time()
        key = self._key(session_id)
        self._setex_raw(key, self.ttl_seconds, json.dumps(session))

    def get_session_language(self, session_id: str) -> Optional[str]:
        session = self.get_session(session_id)
        if session:
            return session.get("teaching_language")
        return None

    def get_ttl(self, session_id: str) -> int:
        """Return remaining TTL seconds for the session in Redis (<= 0 if expired)."""
        key = self._key(session_id)
        return self._ttl_raw(key)

    def clear_session(self, session_id: str) -> bool:
        """Delete session and its history from Redis immediately."""
        key = self._key(session_id)
        deleted = self._delete_raw(key)
        logger.info(f"Cleared session {session_id} from Redis (existed={deleted}).")
        return deleted

    def touch(self, session_id: str) -> bool:
        """Refresh the 30-minute expiration countdown without altering history."""
        key = self._key(session_id)
        return self._expire_raw(key, self.ttl_seconds)


_global_session_manager: Optional[RedisSessionManager] = None


def get_session_manager() -> RedisSessionManager:
    """Return the singleton RedisSessionManager instance."""
    global _global_session_manager
    if _global_session_manager is None:
        _global_session_manager = RedisSessionManager()
    return _global_session_manager
