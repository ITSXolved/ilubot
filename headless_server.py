"""
headless_server.py — Headless Cloud Run / Docker runner for Ilubot 3D Conference UI.

Runs the FastAPI + WebSocket conference server in the foreground, connects
browser audio/text commands to Gemini, and serves the 3D WebGL avatar interface.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import re
import sys

import uvicorn
from core.conference_server import ConferenceServer
from core.session_store import create_session_store
from core.webhook_api import register_webhook_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HeadlessIlubot")

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")

def _teaching_language() -> str:
    """The language picked on the call screen (saved setting) wins; otherwise
    the TEACHING_LANGUAGE env var (Docker / Cloud Run default); else English."""
    try:
        from memory.config_manager import load_api_keys
        saved = str(load_api_keys().get("teaching_language", "") or "").strip()
        if saved:
            return saved
    except Exception:
        pass
    return os.getenv("TEACHING_LANGUAGE", "").strip() or "English"


def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    cfg_file = BASE_DIR / "config" / "api_keys.json"
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            return data.get("gemini_api_key", "").strip()
        except Exception:
            pass
    return ""


def _system_prompt() -> str:
    return (
        "You are Ilubot (Mark-LVI), a warm, intellectually rigorous teacher and critical thinking mentor. "
        "When the user asks a question, NEVER give the direct answer right away. Instead, ask focused, progressive "
        "guiding questions that lead the user step-by-step to discover the real answer within 3–4 conversational turns.\n\n"
        "Core Rules:\n"
        "1. NO DIRECT ANSWERS UP FRONT: Never dispense the final answer, full explanation, or completed solution on Turn 1 or Turn 2, "
        "and never smuggle the punchline or formula inside a hint. Every question must steer directly toward the real answer "
        "(avoid random tangents).\n"
        "2. 3–4 TURN GUIDED DISCOVERY ARC (PER QUESTION):\n"
        "- Turn 1 (Anchor Clue): Ask one accessible question grounded in what the user already observes or knows that points to the first building block of the real answer.\n"
        "- Turn 2 (Build Toward the Mechanism): Build directly on the user's answer—affirm what fits or use a quick counterexample/contrast to redirect any error—and ask the next logical question moving closer to the real answer.\n"
        "- Turn 3 (Synthesis Bridge): Connect what the user just uncovered and ask the narrowing question that invites them to deduce the real answer.\n"
        "- Turn 3–4 (Reach & Confirm the Real Answer): By the 3rd or 4th exchange on that question, once the user reaches the answer (or has worked through the steps), clearly confirm and summarize the real answer in 1–2 concise sentences, tying it to their reasoning—and invite their next question. Do not stall indefinitely past 4 turns on the same question.\n"
        "- Reset to Turn 1 whenever the user asks a new question.\n"
        "3. SHORTCUTS & ERRORS: If the user says 'just tell me' or 'I don't know', warmly refuse the shortcut and narrow the step with a simpler analogy or two contrasting options to choose between. Do not flatter vague guesses—probe why/how or test with a quick edge case.\n"
        "4. DIALOGUE DISCIPLINE: Keep responses spoken-friendly and concise (1–3 sentences max, never a lecture). Ask only ONE sharp, focused question per guiding turn.\n\n"
        "STRICT SCOPE: You only help the student understand their own study material — what is on their screen "
        "or what they show you on camera. You have NO internet access: never search, browse, or talk about news, "
        "weather, celebrities, sports, politics, shopping or other outside topics. If asked, kindly say you can only "
        "help with the material on their screen or in front of the camera.\n"
        f"LANGUAGE: Always speak and teach in {_teaching_language()}, even if the student writes in another language."
    )


# How many stored messages are sent to the model as context each turn.
HISTORY_CONTEXT_MESSAGES = int(os.environ.get("HISTORY_CONTEXT_MESSAGES", "40"))
TEXT_FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite",
                        "gemini-3.5-flash", "gemini-3.8-flash"]
# Per-model deadline: these models swing between ~1s and a minute under load, so
# a stuck one is abandoned for the next fallback instead of stalling the reply.
TEXT_TURN_TIMEOUT_MS = int(os.environ.get("TEXT_TURN_TIMEOUT_MS", "15000"))
START_PROMPT = ("[START] Greet the student warmly in 1-2 short sentences, ask their name, and invite "
                "them to show their study material on the screen or camera. Do not mention this instruction.")
RESUME_PROMPT = ("[RESUME] The student has rejoined the call. Welcome them back in one short sentence and "
                 "pick up where you left off. Do not mention this instruction.")
DEFAULT_GREETING = ("Hello and welcome! I am Ilubot, your study tutor. What is your name? "
                    "Open your study material on the screen or show it to the camera, and we will explore it together.")


class HeadlessSession:
    """Serves every browser conversation. Per-conversation state (history, voice)
    lives in the session store (Redis), keyed by the session id the browser keeps,
    so it survives reloads and server restarts and expires after inactivity."""

    def __init__(self, server: ConferenceServer, store):
        self.server = server
        self.store = store
        self.api_key = get_api_key()
        self.client = None
        self.model_name = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
        # Sessions this process has seen while connected; used to tell a student
        # that their session expired (vs. a brand-new one) when they come back.
        self._seen: set[str] = set()
        self._init_gemini()
        self.server.on_session_event = self._dispatch

    # ── Gemini ────────────────────────────────────────────────────────────────
    def _is_live_model(self) -> bool:
        return "live" in (self.model_name or "").lower()

    def _init_gemini(self) -> None:
        if not self.api_key:
            logger.warning("No GEMINI_API_KEY set. Live AI answers will be limited until configured.")
            return
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            logger.info(f"Gemini client successfully initialized with model {self.model_name}.")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")

    def _contents(self, sid: str, prompt: str) -> list[dict]:
        """Stored history for this session + the new user turn, in Gemini format."""
        contents = [{"role": h["role"], "parts": [{"text": h["text"]}]}
                    for h in self.store.history(sid, HISTORY_CONTEXT_MESSAGES)]
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        while contents and contents[0]["role"] != "user":  # must open with a user turn
            contents.pop(0)
        return contents

    def _text_turn(self, contents: list[dict]) -> str:
        """Stateless text turn over the given history, falling back across models."""
        if not self.client:
            raise RuntimeError("Gemini client is not initialized.")
        primary = [] if self._is_live_model() else [self.model_name]
        seen: set[str] = set()
        models = [m for m in primary + TEXT_FALLBACK_MODELS if not (m in seen or seen.add(m))]
        last_error = None
        for m in models:
            try:
                r = self.client.models.generate_content(
                    model=m, contents=contents,
                    config={"system_instruction": _system_prompt(),
                            "http_options": {"timeout": TEXT_TURN_TIMEOUT_MS}})
                return (r.text or "").strip()
            except Exception as e:
                last_error = e
                if any(w in str(e) for w in ("402", "depleted", "RESOURCE_EXHAUSTED", "401", "403")):
                    raise
                logger.warning(f"Query with model {m} failed: {e}. Trying fallback...")
        if last_error:
            raise last_error
        return ""

    def _live_turn(self, contents: list[dict], voice: str) -> tuple[str, str]:
        """One spoken turn over the Gemini Live API; returns (transcript, wav_base64)."""
        import io
        import wave
        model_name = self.model_name if self.model_name.startswith("models/") else f"models/{self.model_name}"
        config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": _system_prompt(),
            "output_audio_transcription": {},
            "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": voice}}},
        }

        async def _run():
            transcript_parts: list[str] = []
            pcm = bytearray()
            async with asyncio.timeout(12):
                async with self.client.aio.live.connect(model=model_name, config=config) as session:
                    await session.send_client_content(turns=contents, turn_complete=True)
                    async for resp in session.receive():
                        sc = resp.server_content
                        if not sc:
                            continue
                        if sc.output_transcription and sc.output_transcription.text:
                            transcript_parts.append(sc.output_transcription.text)
                        if sc.model_turn:
                            for p in sc.model_turn.parts:
                                if p.inline_data and p.inline_data.data:
                                    pcm.extend(p.inline_data.data)
                        if sc.generation_complete or sc.turn_complete:
                            break
            wav_b64 = ""
            if pcm:
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(pcm)
                wav_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            return "".join(transcript_parts).strip(), wav_b64

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    def _generate_speech(self, text: str, voice: str) -> str:
        """Gemini TTS for a text reply (WAV base64), or "" if every TTS model fails."""
        if not self.client or not text:
            return ""
        clean_text = re.sub(r'[*#_`~>\[\]\(\)]', ' ', text).strip()
        if not clean_text:
            return ""
        for tts_model in ["gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts"]:
            try:
                resp = self.client.models.generate_content(
                    model=tts_model,
                    contents=clean_text,
                    config={
                        "response_modalities": ["AUDIO"],
                        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": voice}}},
                    },
                )
                for p in resp.candidates[0].content.parts:
                    if p.inline_data:
                        return base64.b64encode(p.inline_data.data).decode("utf-8")
            except Exception as e:
                logger.warning(f"TTS with {tts_model} failed ({e}), trying fallback...")
        return ""

    def chat_turn(self, sid: str, text: str, want_audio: bool = True) -> tuple[str, str]:
        """One student turn in session `sid`: generate the reply and save both
        sides to the history. Shared by the WebSocket UI and the webhook API.
        Returns (reply_text, wav_base64 or "")."""
        if not self.client:
            self.api_key = get_api_key()
            self._init_gemini()
        if not self.client:
            raise RuntimeError("GEMINI_API_KEY not configured.")
        reply, audio = self._reply(sid, text, want_audio)
        reply = reply or "I heard you loud and clear."
        # Saved only after a successful reply, so failed turns don't leave
        # dangling user messages in the history.
        self.store.append(sid, "user", text)
        self.store.append(sid, "model", reply)
        return reply, audio

    def _reply(self, sid: str, prompt: str, want_audio: bool = True) -> tuple[str, str]:
        """Generate (text, audio) for `prompt` in the context of this session."""
        voice = self.store.get_meta(sid).get("voice") or "Puck"
        contents = self._contents(sid, prompt)
        if not want_audio:
            return self._text_turn(contents), ""
        if self._is_live_model():
            try:
                text, audio = self._live_turn(contents, voice)
                if text:
                    return text, audio
                logger.warning("Live turn returned no transcript; falling back to text model.")
            except Exception as e:
                logger.error(f"Live turn error: {e}")
        text = self._text_turn(contents)
        return text, self._generate_speech(text, voice)

    # ── Per-session messaging ──────────────────────────────────────────────────
    def _send(self, sid: str, **msg) -> None:
        self.server.send_to(sid, msg)

    def _state(self, sid: str, state: str) -> None:
        self._send(sid, type="state", state=state)

    def _log(self, sid: str, text: str) -> None:
        self._send(sid, type="log", message=text)

    def _content(self, sid: str, title: str, text: str, audio: str = "") -> None:
        msg = {"type": "content", "title": title, "text": text}
        if audio:
            msg["audio"] = audio
        self.server.send_to(sid, msg)

    def _simulate_visemes(self, sid: str, duration: float) -> None:
        """Animate the avatar's mouth when there is no real audio to drive it."""
        import random
        import threading
        import time

        def _stream():
            vowels = ["AA", "E", "I", "O", "U"]
            end_time = time.time() + duration
            while time.time() < end_time:
                self._send(sid, type="viseme", level=round(random.uniform(0.35, 0.95), 3),
                           openness=round(random.uniform(0.4, 0.9), 3),
                           width=round(random.uniform(0.2, 0.7), 3), tag=random.choice(vowels))
                time.sleep(0.065)
            self._send(sid, type="viseme", level=0.0, openness=0.0, width=0.0, tag="")
            self._state(sid, "LISTENING")

        threading.Thread(target=_stream, daemon=True).start()

    # ── Event routing ─────────────────────────────────────────────────────────
    def _dispatch(self, sid: str, action: str, data: dict) -> None:
        if action == "disconnect":
            self._seen.discard(sid)
            return

        existed = self.store.touch(sid)  # resets the 30-min inactivity timer
        if not existed and sid in self._seen:
            self._log(sid, "SYS: Your previous session expired after inactivity, so we are starting fresh.")
        self._seen.add(sid)

        if action == "connect":
            self._on_connect(sid, existed)
        elif action in ("start_call", "wake"):
            self._on_start_call(sid)
        elif action == "leave":
            logger.info(f"[{sid[:8]}] Call ended by client.")
            self._state(sid, "SLEEPING")
            self._log(sid, "SYS: Call ended. Assistant entered sleeping mode.")
        elif action == "interrupt":
            self._state(sid, "LISTENING")
        elif action == "toggle_mute":
            self._send(sid, type="mute", muted=bool(data.get("muted", False)))
        elif action == "voice_changed":
            voice = str(data.get("voice", "") or "Puck")
            logger.info(f"[{sid[:8]}] Voice changed to: {voice}")
            self.store.set_meta(sid, voice=voice)
        elif action == "command":
            text = str(data.get("text", "")).strip()
            if text:
                self._on_text_command(sid, text)

    def _on_connect(self, sid: str, existed: bool) -> None:
        if not existed:
            logger.info(f"[{sid[:8]}] New session.")
            return
        visible = [{"role": "assistant" if h["role"] == "model" else "user", "text": h["text"]}
                   for h in self.store.history(sid) if not h.get("hidden")]
        logger.info(f"[{sid[:8]}] Resumed session with {len(visible)} messages.")
        if visible:
            self._send(sid, type="history", messages=visible)

    def _on_start_call(self, sid: str) -> None:
        import threading
        import time
        logger.info(f"[{sid[:8]}] Conference call started by client.")
        self._state(sid, "LISTENING")
        self._log(sid, "SYS: Call connected. Teacher Ilubot online.")
        if not self.client:
            self.api_key = get_api_key()
            self._init_gemini()

        def _greet():
            time.sleep(0.6)
            resumed = any(not h.get("hidden") for h in self.store.history(sid))
            prompt = RESUME_PROMPT if resumed else START_PROMPT
            greeting, audio = DEFAULT_GREETING, ""
            if self.client:
                try:
                    greeting, audio = self._reply(sid, prompt)
                    greeting = greeting or DEFAULT_GREETING
                    # The trigger is stored hidden: kept for model context, not replayed in the UI.
                    self.store.append(sid, "user", prompt, hidden=True)
                    self.store.append(sid, "model", greeting)
                except Exception as e:
                    logger.error(f"[{sid[:8]}] Greeting generation failed: {e}")
            self._content(sid, "Ilubot", greeting, audio)
            self._state(sid, "SPEAKING")
            if not audio:
                self._simulate_visemes(sid, duration=3.5)

        threading.Thread(target=_greet, daemon=True).start()

    def _on_text_command(self, sid: str, text: str) -> None:
        import threading
        logger.info(f"[{sid[:8]}] User: {text}")
        if not self.client:
            self.api_key = get_api_key()
            self._init_gemini()
        if not self.client:
            self._content(sid, "System", "GEMINI_API_KEY not configured. Please supply an API key.")
            return

        def _generate():
            try:
                self._state(sid, "THINKING")
                reply, audio = self.chat_turn(sid, text)
                logger.info(f"[{sid[:8]}] Reply ready ({len(reply)} chars, audio {len(audio)} b64).")
                self._content(sid, "Ilubot", reply, audio)
                self._state(sid, "SPEAKING")
                if not audio:
                    self._simulate_visemes(sid, duration=min(5.0, max(1.5, len(reply.split()) * 0.28)))
            except Exception as e:
                logger.error(f"[{sid[:8]}] Error querying Gemini: {e}")
                err_str = str(e)
                if "depleted" in err_str or "402" in err_str:
                    user_msg = ("⚠️ Prepayment credits depleted (HTTP 402). Go to https://aistudio.google.com/app/apikey "
                                "and create an API key in a *New Project* (Free Tier) or add billing credits.")
                else:
                    user_msg = f"Could not generate response: {e}"
                self._content(sid, "Error", user_msg)
                self._state(sid, "LISTENING")

        threading.Thread(target=_generate, daemon=True).start()


def main():
    logger.info(f"Starting Ilubot Headless Conference Server on {HOST}:{PORT}")
    server = ConferenceServer(port=PORT, host=HOST)
    _session = HeadlessSession(server, create_session_store())
    register_webhook_routes(server.app, _session)  # /api/v1/*, /healthz, /embed.js

    # Run uvicorn in the main thread (blocking) for Docker / Cloud Run
    uvicorn.run(
        server.app,
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
