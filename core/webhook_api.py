"""
webhook_api.py — HTTP API so any website or backend can talk to Ilubot.

Endpoints (all JSON):
    GET    /healthz                          liveness + session-store check (no auth)
    GET    /embed.js                         drop-in chat widget for any site (no auth)
    POST   /api/v1/chat                      send a student message, get the tutor reply
    GET    /api/v1/sessions/{sid}/history    read a conversation
    DELETE /api/v1/sessions/{sid}            end a conversation now

/api/v1/* needs an API key from ILUBOT_API_KEYS (comma-separated), sent as
"Authorization: Bearer <key>" or "X-API-Key: <key>". With no keys configured
the API is disabled (503), so a fresh container never exposes a free Gemini proxy.

POST /api/v1/chat body:
    {"message": "...",              required, <= 4000 chars
     "session_id": "...",           optional; omitted -> a new session is created
     "audio": false,                optional; true adds "audio_wav_base64" (slower)
     "callback_url": "https://...", optional; -> 202 now, result POSTed there later
     "metadata": {...}}             optional; echoed back untouched

Callbacks are signed: X-Ilubot-Signature = "sha256=" + HMAC_SHA256(WEBHOOK_SECRET,
"<X-Ilubot-Timestamp>.<raw body>"). Callback targets must be https and resolve to
public addresses (no SSRF into the container's network) unless relaxed via env.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from core.conference_server import _SID_RE

logger = logging.getLogger("WebhookAPI")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_MESSAGE_CHARS = 4000


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _check_callback_url(url: str) -> str | None:
    """Returns an error message, or None if the callback target is acceptable."""
    try:
        u = urlparse(url)
    except Exception:
        return "callback_url is not a valid URL"
    allow_http = os.environ.get("WEBHOOK_ALLOW_HTTP", "").lower() == "true"
    if u.scheme != "https" and not (allow_http and u.scheme == "http"):
        return "callback_url must use https"
    if not u.hostname:
        return "callback_url has no host"
    hosts = _env_list("WEBHOOK_CALLBACK_HOSTS")
    if hosts and u.hostname not in hosts:
        return "callback_url host is not in WEBHOOK_CALLBACK_HOSTS"
    if os.environ.get("WEBHOOK_ALLOW_PRIVATE", "").lower() != "true":
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
        except socket.gaierror:
            return "callback_url host does not resolve"
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if not ip.is_global:
                return "callback_url resolves to a private/internal address"
    return None


def _sign(body: bytes, ts: str) -> str:
    secret = os.environ.get("WEBHOOK_SECRET", "").encode()
    return "sha256=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()


async def _deliver(url: str, payload: dict) -> None:
    import httpx
    body = json.dumps(payload).encode()
    for attempt in range(3):
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", "User-Agent": "Ilubot-Webhook/1",
                   "X-Ilubot-Timestamp": ts, "X-Ilubot-Signature": _sign(body, ts)}
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
                r = await c.post(url, content=body, headers=headers)
            if r.status_code < 300:
                return
            logger.warning(f"Callback to {urlparse(url).hostname} got HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"Callback to {urlparse(url).hostname} failed: {e}")
        await asyncio.sleep(2 ** attempt)
    logger.error(f"Giving up on callback {payload.get('request_id')} to {urlparse(url).hostname}")


def register_webhook_routes(app: FastAPI, session) -> None:
    """`session` is the HeadlessSession (provides .store and .chat_turn)."""
    store = session.store
    api_keys = _env_list("ILUBOT_API_KEYS")
    if not api_keys:
        logger.warning("ILUBOT_API_KEYS is not set: /api/v1/* is DISABLED. Set it to enable the webhook API.")
    if not os.environ.get("WEBHOOK_SECRET"):
        logger.warning("WEBHOOK_SECRET is not set: callback signatures cannot be verified by receivers.")
    pending: set[asyncio.Task] = set()  # keep callback tasks referenced until done

    def _auth(request: Request) -> JSONResponse | None:
        if not api_keys:
            return _err(503, "api_disabled", "Set ILUBOT_API_KEYS on the server to enable this API.")
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if not any(hmac.compare_digest(key.encode(), k.encode()) for k in api_keys):
            return _err(401, "unauthorized", "Missing or invalid API key.")
        return None

    @app.get("/healthz")
    async def healthz():
        try:
            await asyncio.to_thread(store.exists, "healthcheck")
            return {"status": "ok", "session_store": store.backend}
        except Exception as e:
            return JSONResponse({"status": "degraded", "session_store": store.backend,
                                 "error": str(e)}, status_code=503)

    @app.get("/embed.js")
    async def embed_js():
        return FileResponse(WEB_DIR / "embed.js", media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=3600"})

    @app.post("/api/v1/chat")
    async def chat(request: Request):
        if (denied := _auth(request)):
            return denied
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return _err(400, "bad_json", "Body must be a JSON object.")

        message = str(body.get("message", "")).strip()
        if not message:
            return _err(400, "missing_message", "'message' is required.")
        if len(message) > MAX_MESSAGE_CHARS:
            return _err(413, "message_too_long", f"'message' is limited to {MAX_MESSAGE_CHARS} characters.")
        sid = str(body.get("session_id") or uuid.uuid4().hex)
        if not _SID_RE.match(sid):
            return _err(400, "bad_session_id", "'session_id' must be 8-64 chars of A-Z a-z 0-9 _ -.")
        want_audio = bool(body.get("audio", False))
        metadata = body.get("metadata")
        callback_url = body.get("callback_url")

        if callback_url and (problem := await asyncio.to_thread(_check_callback_url, str(callback_url))):
            return _err(400, "bad_callback_url", problem)

        await asyncio.to_thread(store.touch, sid)  # creates / extends the 30-min session
        request_id = uuid.uuid4().hex

        async def _run() -> dict:
            reply, audio = await asyncio.to_thread(session.chat_turn, sid, message, want_audio)
            out = {"request_id": request_id, "session_id": sid, "reply": reply}
            if want_audio:
                out["audio_wav_base64"] = audio
            if metadata is not None:
                out["metadata"] = metadata
            return out

        if callback_url:
            async def _background():
                try:
                    payload = {"event": "chat.reply", **(await _run())}
                except Exception as e:
                    logger.error(f"[{sid[:8]}] webhook turn failed: {e}")
                    payload = {"event": "chat.error", "request_id": request_id, "session_id": sid,
                               "error": str(e), **({"metadata": metadata} if metadata is not None else {})}
                await _deliver(str(callback_url), payload)

            task = asyncio.create_task(_background())
            pending.add(task)
            task.add_done_callback(pending.discard)
            return JSONResponse({"request_id": request_id, "session_id": sid, "status": "accepted"},
                                status_code=202)

        try:
            return await _run()
        except Exception as e:
            logger.error(f"[{sid[:8]}] webhook turn failed: {e}")
            return _err(502, "generation_failed", str(e))

    @app.get("/api/v1/sessions/{sid}/history")
    async def history(sid: str, request: Request):
        if (denied := _auth(request)):
            return denied
        if not _SID_RE.match(sid) or not await asyncio.to_thread(store.exists, sid):
            return _err(404, "session_not_found", "No such session (it may have expired).")
        msgs = await asyncio.to_thread(store.history, sid)
        return {"session_id": sid, "messages": [
            {"role": "assistant" if m["role"] == "model" else "user", "text": m["text"], "ts": m["ts"]}
            for m in msgs if not m.get("hidden")]}

    @app.delete("/api/v1/sessions/{sid}")
    async def delete_session(sid: str, request: Request):
        if (denied := _auth(request)):
            return denied
        if not _SID_RE.match(sid):
            return _err(400, "bad_session_id", "Invalid session id.")
        await asyncio.to_thread(store.delete, sid)
        return {"session_id": sid, "deleted": True}
