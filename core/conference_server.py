"""
core/conference_server.py — Local HTTP & WebSocket server for the 3D Video Conference UI.

Serves the 3D WebGL avatar interface, handles real-time viseme and state broadcasting,
and connects conference controls (Mute, Interrupt, Chat, Settings) to the Jarvis backend.
Supports multi-user session isolation and Redis-backed session history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from core.redis_session import get_session_manager, RedisSessionManager

logger = logging.getLogger("ConferenceServer")

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
AVATAR_DIR = BASE_DIR / "avatar"
PORT = int(os.environ.get("PORT", "8085"))
HOST = os.environ.get("HOST", "0.0.0.0")


class ConferenceServer:
    def __init__(self, port: int = PORT, host: str = HOST):
        self.port = int(os.environ.get("PORT", port))
        self.host = host
        self.app = FastAPI(title="Ilubot Conference Server")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.active_websockets: list[WebSocket] = []
        self.session_websockets: dict[str, set[WebSocket]] = {}
        self.ws_to_session: dict[WebSocket, str] = {}
        self.session_manager: RedisSessionManager = get_session_manager()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None

        # Callbacks hooked by JarvisUI or HeadlessSession
        # Supports either (arg) or (session_id, arg) signatures
        self.on_interrupt: Callable[..., None] | None = None
        self.on_mute_toggled: Callable[..., None] | None = None
        self.on_text_command: Callable[..., None] | None = None
        self.on_audio_command: Callable[..., None] | None = None
        self.on_voice_changed: Callable[..., None] | None = None
        self.on_screen_capture: Callable[..., None] | None = None
        self.on_leave_call: Callable[..., None] | None = None
        self.on_start_call: Callable[..., None] | None = None

        self._setup_routes()

    def _dispatch_callback(self, cb: Any, session_id: str, *args: Any) -> None:
        """Dispatch callback supporting both desktop (no session_id) and multi-session (session_id) handlers."""
        if not cb:
            return
        import inspect
        try:
            sig = inspect.signature(cb)
            params = list(sig.parameters.values())
            param_names = [p.name for p in params]
            has_var_positional = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
            if "session_id" in param_names or len(param_names) == len(args) + 1 or has_var_positional:
                cb(session_id, *args)
            else:
                cb(*args)
        except TypeError:
            try:
                cb(*args)
            except Exception as e:
                logger.error(f"[ConferenceServer] Callback dispatch fallback error: {e}")
        except Exception as e:
            try:
                cb(*args)
            except Exception as inner_e:
                logger.error(f"[ConferenceServer] Callback dispatch error: {inner_e} (initial: {e})")

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
        async def get_teaching_language_route(request: Request):
            session_id = request.query_params.get("session_id", "").strip()
            try:
                from memory.config_manager import TEACHING_LANGUAGES, get_teaching_language
                lang = ""
                if session_id:
                    lang = self.session_manager.get_session_language(session_id) or ""
                if not lang:
                    lang = get_teaching_language()
                return {"language": lang, "options": TEACHING_LANGUAGES}
            except Exception:
                return {"language": "English", "options": ["English"]}

        @self.app.post("/api/teaching_language")
        async def set_teaching_language_route(request: Request):
            try:
                body = await request.json()
            except Exception:
                body = {}
            try:
                from memory.config_manager import get_teaching_language, save_teaching_language
                lang = str(body.get("language", "")).strip()[:40]
                session_id = str(body.get("session_id", "")).strip()
                if session_id and lang:
                    self.session_manager.set_session_language(session_id, lang)
                elif lang:
                    save_teaching_language(lang)
                return {"language": lang or get_teaching_language()}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.get("/api/sessions/{session_id}/history")
        async def get_session_history_route(session_id: str):
            try:
                history = self.session_manager.get_history(session_id)
                ttl = self.session_manager.get_ttl(session_id)
                return {
                    "session_id": session_id,
                    "messages": history,
                    "ttl": ttl,
                    "message_count": len(history),
                }
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.delete("/api/sessions/{session_id}")
        async def delete_session_route(session_id: str):
            try:
                cleared = self.session_manager.clear_session(session_id)
                return {"status": "cleared" if cleared else "not_found", "session_id": session_id}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.get("/api/tts")
        async def tts_endpoint(text: str = "", lang: str = "en", session_id: str = ""):
            """Server-side TTS audio streaming (MP3) supporting 16+ languages with auto-chunking."""
            import asyncio
            import re
            import urllib.parse
            import httpx
            from fastapi.responses import Response

            clean_text = text.strip()[:1000]
            if not clean_text:
                return Response(content=b"", media_type="audio/mpeg")

            if not lang or lang.lower() == "english":
                if session_id:
                    sess_lang = self.session_manager.get_session_language(session_id)
                    if sess_lang:
                        lang = sess_lang

            lang_map = {
                "english": "en",
                "arabic": "ar",
                "hindi": "hi",
                "malayalam": "ml",
                "tamil": "ta",
                "urdu": "ur",
                "bengali": "bn",
                "french": "fr",
                "spanish": "es",
                "german": "de",
                "turkish": "tr",
                "indonesian": "id",
                "malay": "ms",
                "chinese": "zh-CN",
                "japanese": "ja",
                "korean": "ko",
            }
            tl = lang_map.get(lang.lower().strip(), "en")

            # Clean and split into <= 150 char chunks to avoid Google TTS 400 Bad Request
            cleaned_phrase = re.sub(r'[*#_`~>\[\]\(\)]', ' ', clean_text)
            cleaned_phrase = re.sub(r'\s+', ' ', cleaned_phrase).strip()
            if not cleaned_phrase:
                return Response(content=b"", media_type="audio/mpeg")

            def _chunk_text(s: str, max_len: int = 150) -> list[str]:
                if len(s) <= max_len:
                    return [s]
                words = s.split(' ')
                chunks = []
                cur = []
                cur_len = 0
                for w in words:
                    if cur_len + len(w) + 1 > max_len and cur:
                        chunks.append(' '.join(cur))
                        cur = [w]
                        cur_len = len(w)
                    else:
                        cur.append(w)
                        cur_len += len(w) + 1
                if cur:
                    chunks.append(' '.join(cur))
                return chunks[:8]

            chunks = _chunk_text(cleaned_phrase)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }

            try:
                async with httpx.AsyncClient(timeout=8.0) as http_client:
                    tasks = [
                        http_client.get(
                            f"https://translate.google.com/translate_tts?ie=UTF-8&tl={tl}&client=tw-ob&q={urllib.parse.quote(c)}",
                            headers=headers
                        )
                        for c in chunks
                    ]
                    resps = await asyncio.gather(*tasks, return_exceptions=True)
                    audio_chunks = []
                    for r in resps:
                        if isinstance(r, httpx.Response) and r.status_code == 200 and len(r.content) > 50:
                            audio_chunks.append(r.content)
                    if audio_chunks:
                        return Response(
                            content=b"".join(audio_chunks),
                            media_type="audio/mpeg",
                            headers={
                                "Cache-Control": "public, max-age=86400",
                                "Accept-Ranges": "bytes",
                            },
                        )
            except Exception as e:
                logger.error(f"[ConferenceServer] TTS fetch error: {e}")
            return Response(content=b"", status_code=502)

        @self.app.get("/avatar/{name}")
        async def get_avatar(name: str):
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

        @self.app.on_event("startup")
        async def on_startup():
            try:
                self._loop = asyncio.get_running_loop()
            except Exception:
                pass

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            try:
                self._loop = asyncio.get_running_loop()
            except Exception:
                pass
            await ws.accept()
            raw_session_id = ws.query_params.get("session_id") or ws.headers.get("x-session-id")
            session_id = (raw_session_id.strip() if raw_session_id else "") or f"sess_{uuid.uuid4().hex[:12]}"

            # Initialize session tracking
            self.session_manager.create_or_get_session(session_id)
            if session_id not in self.session_websockets:
                self.session_websockets[session_id] = set()
            self.session_websockets[session_id].add(ws)
            self.ws_to_session[ws] = session_id
            self.active_websockets.append(ws)

            try:
                # Send session handshake ack
                await ws.send_text(json.dumps({
                    "type": "session_init",
                    "session_id": session_id,
                    "ttl": self.session_manager.get_ttl(session_id),
                }))

                # Send existing history if user reconnects within 30m window
                existing_history = self.session_manager.get_history(session_id)
                if existing_history:
                    await ws.send_text(json.dumps({
                        "type": "history",
                        "session_id": session_id,
                        "messages": existing_history,
                    }))

                while True:
                    text = await ws.receive_text()
                    try:
                        data = json.loads(text)
                        action = data.get("action")
                        self.session_manager.touch(session_id)

                        if action == "interrupt":
                            self._dispatch_callback(self.on_interrupt, session_id)
                        elif action == "toggle_mute":
                            self._dispatch_callback(self.on_mute_toggled, session_id, bool(data.get("muted", False)))
                        elif action == "command":
                            cmd = data.get("text", "").strip()
                            if cmd:
                                self._dispatch_callback(self.on_text_command, session_id, cmd)
                        elif action == "audio_command":
                            audio_b64 = data.get("audio", "")
                            mime = data.get("mime", "audio/webm")
                            if audio_b64:
                                self._dispatch_callback(self.on_audio_command, session_id, audio_b64, mime)
                        elif action == "voice_changed":
                            self._dispatch_callback(self.on_voice_changed, session_id, data.get("voice", ""))
                        elif action == "screen_capture":
                            self._dispatch_callback(self.on_screen_capture, session_id)
                        elif action == "leave":
                            self._dispatch_callback(self.on_leave_call, session_id)
                        elif action in ("start_call", "wake"):
                            self._dispatch_callback(self.on_start_call, session_id)
                    except Exception as e:
                        print(f"[ConferenceServer] WS message error: {e}")
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            finally:
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)
                if session_id in self.session_websockets and ws in self.session_websockets[session_id]:
                    self.session_websockets[session_id].remove(ws)
                    if not self.session_websockets[session_id]:
                        self.session_websockets.pop(session_id, None)
                self.ws_to_session.pop(ws, None)

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

    # ── Session-Targeted Messaging ─────────────────────────────────────────────

    def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        """Thread-safe targeted send to WebSockets belonging to a specific session."""
        if not self._loop or not self._loop.is_running():
            try:
                self._loop = asyncio.get_running_loop()
            except Exception:
                pass
        if not self._loop or not self._loop.is_running():
            logger.warning("[ConferenceServer] Event loop not running; cannot send_to_session")
            return
        sockets = list(self.session_websockets.get(session_id, set()))
        if not sockets and (session_id == "default" or len(self.session_websockets) <= 1):
            # Fallback to all active sockets if session not specifically registered
            sockets = list(self.active_websockets)
        if not sockets:
            return

        text = json.dumps(message)

        async def _send():
            dead = []
            for ws in sockets:
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)
                if session_id in self.session_websockets and ws in self.session_websockets[session_id]:
                    self.session_websockets[session_id].remove(ws)
                self.ws_to_session.pop(ws, None)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception as e:
            logger.error(f"[ConferenceServer] send_to_session dispatch error: {e}")

    def send_state(self, session_id: str, state: str) -> None:
        self.send_to_session(session_id, {"type": "state", "state": state})

    def send_audio_level(self, session_id: str, level: float) -> None:
        self.send_to_session(session_id, {"type": "audio_level", "level": round(float(level), 3)})

    def send_viseme(self, session_id: str, level: float, openness: float, width: float, tag: str = "") -> None:
        self.send_to_session(session_id, {
            "type": "viseme",
            "level": round(float(level), 3),
            "openness": round(float(openness), 3),
            "width": round(float(width), 3),
            "tag": str(tag or "")
        })

    def send_log(self, session_id: str, text: str) -> None:
        self.send_to_session(session_id, {"type": "log", "message": text})

    def send_content(self, session_id: str, title: str, text: str) -> None:
        self.send_to_session(session_id, {"type": "content", "title": title, "text": text})

    def send_mute(self, session_id: str, muted: bool) -> None:
        self.send_to_session(session_id, {"type": "mute", "muted": muted})

    # ── Global Broadcast Messaging (Backward Compatibility) ─────────────────────

    def broadcast(self, message: dict[str, Any]) -> None:
        """Thread-safe broadcast to all connected WebSocket clients across all sessions."""
        if not self._loop or not self._loop.is_running():
            try:
                self._loop = asyncio.get_running_loop()
            except Exception:
                pass
        if not self._loop or not self._loop.is_running():
            return

        text = json.dumps(message)

        async def _send():
            dead = []
            for ws in list(self.active_websockets):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)
                session_id = self.ws_to_session.pop(ws, None)
                if session_id and session_id in self.session_websockets and ws in self.session_websockets[session_id]:
                    self.session_websockets[session_id].remove(ws)

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

    def broadcast_content(self, title: str, text: str) -> None:
        self.broadcast({"type": "content", "title": title, "text": text})

    def broadcast_mute(self, muted: bool) -> None:
        self.broadcast({"type": "mute", "muted": muted})


_instance: ConferenceServer | None = None


def get_conference_server() -> ConferenceServer:
    global _instance
    if _instance is None:
        _instance = ConferenceServer()
        _instance.start()
    return _instance
