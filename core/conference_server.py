"""
core/conference_server.py — Local HTTP & WebSocket server for the 3D Video Conference UI.

Serves the 3D WebGL avatar interface, handles real-time viseme and state broadcasting,
and connects conference controls (Mute, Interrupt, Chat, Settings) to the Jarvis backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
AVATAR_DIR = BASE_DIR / "avatar"
PORT = int(os.environ.get("PORT", "8085"))
HOST = os.environ.get("HOST", "0.0.0.0")

# Client-supplied session ids are used as Redis key parts, so keep them tame.
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def allowed_origins() -> list[str]:
    """Sites allowed to call the API, embed the widget and open the WebSocket.
    ALLOWED_ORIGINS is comma-separated, e.g. "https://school.example,https://lms.example";
    unset or "*" allows any site."""
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    return origins or ["*"]


class ConferenceServer:
    def __init__(self, port: int = PORT, host: str = HOST):
        self.port = int(os.environ.get("PORT", port))
        self.host = host
        self.app = FastAPI(title="Ilubot Conference Server")
        self.allowed_origins = allowed_origins()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=self.allowed_origins,
            allow_credentials=False,  # API auth is by header key, never cookies
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Which sites may show the tutor in an <iframe> (the embed widget).
        frame_ancestors = "*" if "*" in self.allowed_origins \
            else "'self' " + " ".join(self.allowed_origins)

        @self.app.middleware("http")
        async def _frame_policy(request: Request, call_next):
            resp = await call_next(request)
            resp.headers.setdefault("Content-Security-Policy", f"frame-ancestors {frame_ancestors}")
            return resp
        self.active_websockets: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None

        # Callbacks hooked by JarvisUI
        self.on_interrupt: Callable[[], None] | None = None
        self.on_mute_toggled: Callable[[bool], None] | None = None
        self.on_text_command: Callable[[str], None] | None = None
        self.on_voice_changed: Callable[[str], None] | None = None
        self.on_screen_capture: Callable[[], None] | None = None
        self.on_leave_call: Callable[[], None] | None = None
        self.on_start_call: Callable[[], None] | None = None

        # Session mode (headless server): when set, EVERY client message is routed
        # here as (sid, action, data) instead of the legacy callbacks above, plus
        # synthetic "connect" / "disconnect" actions. Reply with send_to(sid, ...).
        self.on_session_event: Callable[[str, str, dict], None] | None = None
        self.sessions: dict[str, WebSocket] = {}

        self._setup_routes()

    def _setup_routes(self) -> None:
        @self.app.get("/")
        async def index():
            index_file = WEB_DIR / "index.html"
            if index_file.exists():
                return FileResponse(
                    index_file,
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
                )
            return HTMLResponse("<h1>Conference interface not found</h1>", status_code=404)

        @self.app.get("/api/stage_style")
        async def get_stage_style_route():
            try:
                from memory.config_manager import get_stage_style
                return {"style": get_stage_style()}
            except Exception:
                return {"style": "brain"}

        @self.app.post("/api/stage_style")
        async def set_stage_style_route(request: Request):
            try:
                body = await request.json()
            except Exception:
                body = {}
            try:
                from memory.config_manager import save_stage_style
                return {"style": save_stage_style(str(body.get("style", "")))}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.get("/api/teaching_language")
        async def get_teaching_language_route():
            try:
                from memory.config_manager import TEACHING_LANGUAGES, get_teaching_language
                return {"language": get_teaching_language(), "options": TEACHING_LANGUAGES}
            except Exception:
                return {"language": "English", "options": ["English"]}

        @self.app.post("/api/teaching_language")
        async def set_teaching_language_route(request: Request):
            # Saved before the call starts; the session reads it when it
            # connects, so the whole conversation runs in this language.
            try:
                body = await request.json()
            except Exception:
                body = {}
            try:
                from memory.config_manager import get_teaching_language, save_teaching_language
                lang = str(body.get("language", "")).strip()[:40]
                if lang:
                    save_teaching_language(lang)
                return {"language": get_teaching_language()}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.get("/avatar/{name}")
        async def get_avatar(name: str):
            # Serve the model the page asked for; only plain .glb names inside
            # AVATAR_DIR are allowed (no paths, no traversal).
            glb_file = AVATAR_DIR / name
            if (name != Path(name).name or not name.lower().endswith(".glb")
                    or not glb_file.is_file()):
                glb_file = AVATAR_DIR / "ZainAI.glb"
            if glb_file.exists():
                return FileResponse(
                    glb_file,
                    media_type="model/gltf-binary",
                    filename=glb_file.name
                )
            return HTMLResponse("Avatar file not found", status_code=404)

        if WEB_DIR.exists():
            self.app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            # Browsers always send Origin. The embedded tutor page is served from
            # this server, so its own origin is always fine; other sites' pages
            # opening the socket directly must be on ALLOWED_ORIGINS.
            origin = ws.headers.get("origin", "")
            if not self._origin_ok(origin, ws.headers.get("host", "")):
                print(f"[ConferenceServer] Rejected WebSocket from origin {origin!r}")
                await ws.close(code=4003)
                return
            await ws.accept()
            # headless_server.py runs uvicorn itself (not via start()), so the
            # serving loop must be captured here or broadcast() drops everything.
            self._loop = asyncio.get_running_loop()
            session_mode = self.on_session_event is not None

            sid = ws.query_params.get("sid", "")
            if not _SID_RE.match(sid):
                sid = uuid.uuid4().hex

            # Every connected tab plays each reply, which stacks into echoes.
            # Newest tab wins (per session in session mode, globally otherwise);
            # close code 4000 tells older tabs not to auto-reconnect and steal it back.
            stale = ([self.sessions[sid]] if sid in self.sessions else []) if session_mode \
                else list(self.active_websockets)
            for old in stale:
                try:
                    await old.close(code=4000, reason="Session opened in another tab")
                except Exception:
                    pass
                if old in self.active_websockets:
                    self.active_websockets.remove(old)
            self.active_websockets.append(ws)
            self.sessions[sid] = ws
            await ws.send_text(json.dumps({"type": "session", "sid": sid}))

            if session_mode:
                await asyncio.to_thread(self.on_session_event, sid, "connect", {})
            try:
                while True:
                    text = await ws.receive_text()
                    try:
                        data = json.loads(text)
                        action = data.get("action")
                        if session_mode:
                            # Off the event loop: handlers touch Redis (blocking I/O).
                            await asyncio.to_thread(self.on_session_event, sid, action, data)
                        elif action == "interrupt" and self.on_interrupt:
                            self.on_interrupt()
                        elif action == "toggle_mute" and self.on_mute_toggled:
                            self.on_mute_toggled(bool(data.get("muted", False)))
                        elif action == "command" and self.on_text_command:
                            cmd = data.get("text", "").strip()
                            if cmd:
                                self.on_text_command(cmd)
                        elif action == "voice_changed" and self.on_voice_changed:
                            self.on_voice_changed(data.get("voice", ""))
                        elif action == "screen_capture" and self.on_screen_capture:
                            self.on_screen_capture()
                        elif action == "leave" and self.on_leave_call:
                            self.on_leave_call()
                        elif action in ("start_call", "wake") and self.on_start_call:
                            self.on_start_call()
                    except Exception as e:
                        print(f"[ConferenceServer] WS message error: {e}")
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            finally:
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)
                if self.sessions.get(sid) is ws:
                    del self.sessions[sid]
                    if session_mode:
                        try:
                            await asyncio.to_thread(self.on_session_event, sid, "disconnect", {})
                        except Exception as e:
                            print(f"[ConferenceServer] disconnect handler error: {e}")

    def start(self) -> None:
        """Start the conference HTTP + WS server in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return

        def _run():
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False
            )
            self._server = uvicorn.Server(config)
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._server.serve())

        self._thread = threading.Thread(target=_run, daemon=True, name="ConferenceServer")
        self._thread.start()
        print(f"[ConferenceServer] Running on http://{self.host}:{self.port}")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

    def broadcast(self, message: dict[str, Any]) -> None:
        """Thread-safe broadcast to all connected WebSocket clients."""
        if not self.active_websockets or not self._loop or not self._loop.is_running():
            return

        text = json.dumps(message)

        async def _send():
            dead = []
            for ws in self.active_websockets:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception:
            pass

    def _origin_ok(self, origin: str, host: str) -> bool:
        if "*" in self.allowed_origins or not origin:
            return True
        if urlparse(origin).netloc == host:
            return True
        return origin.rstrip("/") in self.allowed_origins

    def send_to(self, sid: str, message: dict[str, Any]) -> None:
        """Thread-safe send to the one tab currently holding session `sid`."""
        ws = self.sessions.get(sid)
        if ws is None or not self._loop or not self._loop.is_running():
            return
        text = json.dumps(message)

        async def _send():
            try:
                await ws.send_text(text)
            except Exception:
                pass

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception:
            pass

    def broadcast_state(self, state: str) -> None:
        self.broadcast({"type": "state", "state": state})

    def broadcast_audio_level(self, level: float) -> None:
        self.broadcast({"type": "audio_level", "level": round(float(level), 3)})

    def broadcast_viseme(self, level: float, openness: float, width: float, tag: str = "") -> None:
        self.broadcast({
            "type": "viseme",
            "level": round(float(level), 3),
            "openness": round(float(openness), 3),
            "width": round(float(width), 3),
            "tag": str(tag or "")
        })

    def broadcast_log(self, text: str) -> None:
        self.broadcast({"type": "log", "message": text})

    def broadcast_content(self, title: str, text: str, audio: str = "") -> None:
        payload = {"type": "content", "title": title, "text": text}
        if audio:
            payload["audio"] = audio
        self.broadcast(payload)

    def broadcast_mute(self, muted: bool) -> None:
        self.broadcast({"type": "mute", "muted": muted})


_instance: ConferenceServer | None = None


def get_conference_server() -> ConferenceServer:
    global _instance
    if _instance is None:
        _instance = ConferenceServer()
        _instance.start()
    return _instance
