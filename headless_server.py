"""
headless_server.py — Headless Cloud Run / Docker runner for Ilubot 3D Conference UI.

Runs the FastAPI + WebSocket conference server in the foreground, connects
browser audio/text commands to Gemini, manages isolated user sessions in Redis
with 30-minute automatic TTL expiration, and serves the 3D WebGL avatar interface.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from core.conference_server import ConferenceServer
from core.redis_session import get_session_manager, RedisSessionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HeadlessIlubot")

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")


def _teaching_language(session_id: Optional[str] = None) -> str:
    """Returns session-specific language, or global saved setting, or TEACHING_LANGUAGE env, or English."""
    if session_id:
        try:
            sm = get_session_manager()
            lang = sm.get_session_language(session_id)
            if lang:
                return lang
        except Exception:
            pass
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


def build_system_instruction(language: str) -> str:
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
        f"LANGUAGE: Always speak and teach in {language}, even if the student writes in another language."
    )


class HeadlessSession:
    """Manages multi-user isolated sessions, chat streams, and Redis history persistence."""

    def __init__(self, server: ConferenceServer):
        self.server = server
        self.session_manager: RedisSessionManager = get_session_manager()
        self.api_key = get_api_key()
        self.client: Any = None
        self.model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.user_chats: dict[str, Any] = {}
        self._viseme_cancel_events: dict[str, threading.Event] = {}

        self._init_gemini_client()
        self._wire_callbacks()

    def _init_gemini_client(self) -> None:
        if not self.api_key:
            self.api_key = get_api_key()
        if not self.api_key:
            logger.warning("No GEMINI_API_KEY set. Live AI answers will be limited until configured.")
            return
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            logger.info("Gemini SDK client successfully initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini SDK client: {e}")

    def _get_or_create_chat(self, session_id: str, force_new: bool = False) -> Any:
        """Create or retrieve a per-session Gemini chat, restoring prior turns from Redis if available."""
        if not self.client:
            self._init_gemini_client()
        if not self.client:
            return None

        if not force_new and session_id in self.user_chats:
            return self.user_chats[session_id]

        lang = _teaching_language(session_id)
        system_instruction = build_system_instruction(lang)

        # Retrieve prior conversation turns stored in Redis (up to 30 min old)
        history = self.session_manager.get_gemini_history(session_id) if not force_new else []

        try:
            chat = self.client.chats.create(
                model=self.model_name,
                config={"system_instruction": system_instruction},
                history=history if history else None,
            )
            self.user_chats[session_id] = chat
            logger.info(f"Initialized Gemini chat for session {session_id} (restored {len(history)} turns, lang: {lang})")
            return chat
        except Exception as e:
            logger.error(f"Failed to create chat for session {session_id}: {e}")
            return None

    def _wire_callbacks(self) -> None:
        self.server.on_start_call = self._on_start_call
        self.server.on_leave_call = self._on_leave_call
        self.server.on_text_command = self._on_text_command
        self.server.on_interrupt = self._on_interrupt
        self.server.on_mute_toggled = self._on_mute_toggled

    def _on_start_call(self, session_id: str = "default") -> None:
        logger.info(f"[{session_id}] Conference call started by client.")
        self.server.send_state(session_id, "LISTENING")
        self.server.send_log(session_id, "SYS: Call connected. Teacher Ilubot online.")

        # Check if returning user within 30-min window
        prior_history = self.session_manager.get_history(session_id)
        chat = self._get_or_create_chat(session_id, force_new=False)

        def _delayed_greeting():
            time.sleep(0.5)
            if prior_history:
                # User returned within 30-minute window
                greeting = (
                    "Welcome back! I have your previous questions and notes ready. "
                    "We can continue right where we left off, or you can explore new study material."
                )
                self.server.send_log(session_id, f"SYS: Restored {len(prior_history)} previous messages from Redis.")
            else:
                greeting = (
                    "Hello and welcome! I am Ilubot, your study tutor. What is your name? "
                    "Open your study material on the screen or show it to the camera, and we will explore it together."
                )
                if chat:
                    try:
                        r = chat.send_message(
                            "[START] Greet the student warmly in 1-2 short sentences, ask their name, and invite "
                            "them to show their study material on the screen or camera. Do not mention this instruction."
                        )
                        greeting = (r.text or "").strip() or greeting
                    except Exception as e:
                        logger.error(f"[{session_id}] Greeting generation failed: {e}")

            # Persist greeting to session history (30 min TTL)
            self.session_manager.add_message(session_id, "model", greeting, name="Ilubot")

            self.server.send_content(session_id, "Ilubot", greeting)
            self.server.send_state(session_id, "SPEAKING")
            self._simulate_visemes(session_id, duration=3.5)

        threading.Thread(target=_delayed_greeting, daemon=True).start()

    def _on_leave_call(self, session_id: str = "default") -> None:
        logger.info(f"[{session_id}] Conference call ended by client.")
        self._cancel_visemes(session_id)
        self.server.send_state(session_id, "SLEEPING")
        self.server.send_log(session_id, "SYS: Call ended. Assistant entered sleeping mode.")

    def _on_interrupt(self, session_id: str = "default") -> None:
        logger.info(f"[{session_id}] Interrupt requested.")
        self._cancel_visemes(session_id)
        self.server.send_state(session_id, "LISTENING")

    def _on_mute_toggled(self, session_id: str = "default", muted: bool = False, *args: Any) -> None:
        logger.info(f"[{session_id}] Mute toggled: {muted}")
        self.server.send_mute(session_id, muted)

    def _on_text_command(self, session_id: str = "default", text: str = "", *args: Any) -> None:
        text = text.strip()
        if not text:
            return

        logger.info(f"[{session_id}] User text command: {text}")

        # Record user message in Redis (refreshes 30 min TTL)
        self.session_manager.add_message(session_id, "user", text)

        chat = self._get_or_create_chat(session_id)
        if not self.client or not chat:
            self.server.send_content(session_id, "System", "GEMINI_API_KEY not configured. Please supply an API key.")
            return

        def _generate():
            try:
                self.server.send_state(session_id, "THINKING")
                response = chat.send_message(text)
                reply = response.text or "I heard you loud and clear."

                # Store assistant response in Redis (refreshes 30 min TTL)
                self.session_manager.add_message(session_id, "model", reply, name="Ilubot")

                self.server.send_content(session_id, "Ilubot", reply)
                self.server.send_state(session_id, "SPEAKING")
                self._simulate_visemes(session_id, duration=min(5.0, max(1.5, len(reply.split()) * 0.28)))
            except Exception as e:
                logger.error(f"[{session_id}] Error querying Gemini: {e}")
                self.server.send_content(session_id, "Error", f"Could not generate response: {e}")
                self.server.send_state(session_id, "LISTENING")

        threading.Thread(target=_generate, daemon=True).start()

    def _cancel_visemes(self, session_id: str) -> None:
        ev = self._viseme_cancel_events.get(session_id)
        if ev:
            ev.set()

    def _simulate_visemes(self, session_id: str, duration: float) -> None:
        """Stream conversational viseme pulses to animate the 3D mouth while speaking for this session."""
        self._cancel_visemes(session_id)
        cancel_event = threading.Event()
        self._viseme_cancel_events[session_id] = cancel_event

        def _stream():
            vowels = ["AA", "E", "I", "O", "U"]
            end_time = time.time() + duration
            while time.time() < end_time and not cancel_event.is_set():
                lvl = random.uniform(0.35, 0.95)
                op = random.uniform(0.4, 0.9)
                wd = random.uniform(0.2, 0.7)
                tag = random.choice(vowels)
                self.server.send_viseme(session_id, lvl, op, wd, tag)
                time.sleep(0.065)
            self.server.send_viseme(session_id, 0.0, 0.0, 0.0, "")
            if not cancel_event.is_set():
                self.server.send_state(session_id, "LISTENING")

        threading.Thread(target=_stream, daemon=True).start()


def main():
    logger.info(f"Starting Ilubot Headless Conference Server on {HOST}:{PORT}")
    server = ConferenceServer(port=PORT, host=HOST)
    _session = HeadlessSession(server)

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
