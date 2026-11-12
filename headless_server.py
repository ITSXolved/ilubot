"""
headless_server.py — Headless Cloud Run / Docker runner for Ilubot 3D Conference UI.

Runs the FastAPI + WebSocket conference server in the foreground, connects
browser audio/text commands to Gemini, and serves the 3D WebGL avatar interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sys

import uvicorn
from core.conference_server import ConferenceServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HeadlessIlubot")

BASE_DIR = Path(__file__).resolve().parent
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


class HeadlessSession:
    def __init__(self, server: ConferenceServer):
        self.server = server
        self.api_key = get_api_key()
        self.client = None
        self.chat = None
        self.model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._init_gemini()
        self._wire_callbacks()

    def _init_gemini(self) -> None:
        if not self.api_key:
            logger.warning("No GEMINI_API_KEY set. Live AI answers will be limited until configured.")
            return
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            self.chat = self.client.chats.create(
                model=self.model_name,
                config={
                    "system_instruction": (
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
                }
            )
            logger.info("Gemini client successfully initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")

    def _wire_callbacks(self) -> None:
        self.server.on_start_call = self._on_start_call
        self.server.on_leave_call = self._on_leave_call
        self.server.on_text_command = self._on_text_command
        self.server.on_interrupt = self._on_interrupt
        self.server.on_mute_toggled = self._on_mute_toggled

    def _on_start_call(self) -> None:
        logger.info("Conference call started by client.")
        self.server.broadcast_state("LISTENING")
        self.server.broadcast_log("SYS: Call connected. Teacher Ilubot online.")

        # Fresh chat per call so a language picked on the call screen applies.
        if not self.api_key:
            self.api_key = get_api_key()
        self._init_gemini()

        def _delayed_greeting():
            import time
            time.sleep(0.6)
            greeting = ("Hello and welcome! I am Ilubot, your study tutor. What is your name? "
                        "Open your study material on the screen or show it to the camera, and we will explore it together.")
            if self.chat:
                try:
                    r = self.chat.send_message(
                        "[START] Greet the student warmly in 1-2 short sentences, ask their name, and invite "
                        "them to show their study material on the screen or camera. Do not mention this instruction.")
                    greeting = (r.text or "").strip() or greeting
                except Exception as e:
                    logger.error(f"Greeting generation failed: {e}")
            self.server.broadcast_content("Ilubot", greeting)
            self.server.broadcast_state("SPEAKING")
            self._simulate_visemes(duration=3.5)

        import threading
        threading.Thread(target=_delayed_greeting, daemon=True).start()

    def _on_leave_call(self) -> None:
        logger.info("Conference call ended by client.")
        self.server.broadcast_state("SLEEPING")
        self.server.broadcast_log("SYS: Call ended. Assistant entered sleeping mode.")

    def _on_interrupt(self) -> None:
        logger.info("Interrupt requested.")
        self.server.broadcast_state("LISTENING")

    def _on_mute_toggled(self, muted: bool) -> None:
        logger.info(f"Mute toggled: {muted}")
        self.server.broadcast_mute(muted)

    def _on_text_command(self, text: str) -> None:
        logger.info(f"User text command: {text}")
        if not self.client or not self.chat:
            self.api_key = get_api_key()
            self._init_gemini()

        if not self.client:
            self.server.broadcast_content("System", "GEMINI_API_KEY not configured. Please supply an API key.")
            return

        def _generate():
            try:
                self.server.broadcast_state("THINKING")
                response = self.chat.send_message(text)
                reply = response.text or "I heard you loud and clear."
                self.server.broadcast_content("Ilubot", reply)
                self.server.broadcast_state("SPEAKING")
                self._simulate_visemes(duration=min(5.0, max(1.5, len(reply.split()) * 0.28)))
            except Exception as e:
                logger.error(f"Error querying Gemini: {e}")
                self.server.broadcast_content("Error", f"Could not generate response: {e}")
                self.server.broadcast_state("LISTENING")

        import threading
        threading.Thread(target=_generate, daemon=True).start()

    def _simulate_visemes(self, duration: float) -> None:
        """Stream conversational viseme pulses to animate the 3D mouth while speaking."""
        import random
        import time

        def _stream():
            vowels = ["AA", "E", "I", "O", "U"]
            end_time = time.time() + duration
            while time.time() < end_time:
                lvl = random.uniform(0.35, 0.95)
                op = random.uniform(0.4, 0.9)
                wd = random.uniform(0.2, 0.7)
                tag = random.choice(vowels)
                self.server.broadcast_viseme(lvl, op, wd, tag)
                time.sleep(0.065)
            self.server.broadcast_viseme(0.0, 0.0, 0.0, "")
            self.server.broadcast_state("LISTENING")

        import threading
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
