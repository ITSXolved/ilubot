# Integrating Ilubot into Study-Material Applications

This guide explains how to integrate **Ilubot**—a real-time Socratic AI tutor with a 3D/HUD conference UI, multi-language teaching support, and screen/camera awareness—into any study-material-providing application (Learning Management Systems, PDF/e-book readers, flashcard & quiz apps, lecture video portals, coding platforms, or mobile/desktop study tools).

---

## 1. How Ilubot Works as a Study Tutor

Ilubot is designed around a **3–4 Turn Socratic Guided Discovery** persona (`headless_server.py`, `core/prompt.txt`, `main.py`):

- **No direct homework answers up front**: Never gives away final solutions on Turn 1 or Turn 2; asks one focused, progressive guiding question per turn and confirms the answer by Turn 3–4.
- **Strict educational scope**: In `main.py`, `RESTRICTED_MODE = True` withholds web search, file control, app launching, and messaging tools (`ALLOWED_TOOLS = {"screen_process", "close_camera", "shutdown_jarvis"}`). In `headless_server.py`, the Gemini system instruction enforces a screen/material-only scope with no internet browsing.
- **Single-language instruction**: Enforces one chosen `teaching_language` (`English`, `Arabic`, `Hindi`, `Malayalam`, `Tamil`, `Urdu`, `Bengali`, `French`, `Spanish`, `German`, `Turkish`, `Indonesian`, `Malay`, `Chinese`, `Japanese`, `Korean`, or any custom language) regardless of the language of the study material.

Ilubot runs in two primary modes:

| Mode | Entry Point | Default Port | Best For |
|---|---|---|---|
| **Headless Web / Cloud Server** | `headless_server.py` + `core/conference_server.py` | `8080` (`PORT` env) | Web apps, LMS platforms, SaaS EdTech, Docker / Google Cloud Run |
| **Desktop Live Companion** | `main.py` + `ui.py` | `8085` (stage) / `8000→8001` (dashboard) | Studying across third-party desktop apps, local PDFs, and physical notebooks via webcam |

---

## 2. Quick Start: Running Ilubot as a Study Service

### Option A — Docker / Docker Compose
```bash
export GEMINI_API_KEY="your_gemini_api_key"
docker compose up --build
```
Ilubot is now available at `http://localhost:8080` (WebSocket at `ws://localhost:8080/ws`).

### Option B — Python (Local or Sidecar Process)
```bash
pip install -r requirements-docker.txt
export GEMINI_API_KEY="your_gemini_api_key"
export TEACHING_LANGUAGE="English"        # optional default
export GEMINI_MODEL="gemini-2.5-flash"    # optional override
export PORT=8080
python headless_server.py
```

### Option C — Google Cloud Run
See [`DEPLOY_CLOUD_RUN.md`](DEPLOY_CLOUD_RUN.md) for the single-command Cloud Run deployment (`--session-affinity` recommended for WebSocket connections).

> **Important Browser Requirement**: When embedding in a production web app, serve Ilubot over **HTTPS / WSS**. Modern browsers block microphone (`SpeechRecognition`) and camera (`getUserMedia`) access on non-HTTPS origins (except `localhost`).

---

## 3. Integration Patterns

Choose the pattern that matches how much control you have over the host study application:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Study-Material Application (Host)                     │
│   (LMS, PDF Reader, Video Lecture Portal, Quiz App, E-Book, Mobile)     │
└───────────────┬─────────────────────────────────────────┬───────────────┘
                │                                         │
   ┌────────────▼────────────┐               ┌────────────▼────────────┐
   │ Pattern A: <iframe>     │               │ Pattern B: Custom UI    │
   │ Embed web/index.html    │               │ Direct REST + /ws API   │
   │ side-by-side with notes │               │ inside your own UI      │
   └────────────┬────────────┘               └────────────┬────────────┘
                │                                         │
                └────────────────────┬────────────────────┘
                                     ▼
                ┌─────────────────────────────────────────┐
                │   Ilubot Headless Server (FastAPI + WS) │
                │   headless_server.py / conference_server │
                └─────────────────────────────────────────┘

   (For closed-source study apps where you cannot modify code: run
    Pattern C: Desktop Companion via `python main.py` + screen_process)
```

---

### Pattern A: Embeddable Split-Screen Widget (`<iframe>`)

Ideal for web-based LMS portals, digital textbooks, and course viewers where you want Ilubot's full 3D avatar / Neural Brain / HUD UI, built-in voice recognition, TTS, and chat drawer with minimal frontend code.

`web/index.html` includes a responsive mobile/narrow-pane layout (`@media (max-width: 640px)`), so it fits cleanly inside a 360px–440px side panel alongside your study material.

```html
<div class="study-workspace" style="display: flex; height: 100vh;">
  <!-- Left: Your Study Material (PDF, HTML lesson, video, quiz) -->
  <section class="study-content" style="flex: 1; overflow: auto;">
    <!-- ... -->
  </section>

  <!-- Right: Embedded Ilubot Tutor -->
  <aside style="width: 400px; max-width: 100%; border-left: 1px solid #1e293b;">
    <iframe
      id="ilubot-frame"
      src="https://ilubot.yourdomain.com/"
      allow="microphone; camera; autoplay"
      style="width: 100%; height: 100%; border: none;"
      title="Ilubot Study Tutor"
    ></iframe>
  </aside>
</div>
```

**Checklist for `<iframe>` embeds:**
1. Include `allow="microphone; camera; autoplay"` on the `<iframe>`.
2. Ensure your reverse proxy does **not** set `X-Frame-Options: DENY` (use `Content-Security-Policy: frame-ancestors https://your-study-app.com` instead).
3. Select the student's preferred teaching language before the call starts via `POST /api/teaching_language` (or let the student pick it on Ilubot's call screen).
4. Choose a lightweight visual style (`brain` or `ilubot`) via `POST /api/stage_style` for embedded sidebars, or `avatar` when you want the Three.js `.glb` 3D character.

---

### Pattern B: Custom Study UI via REST + WebSocket API

Ideal when your study application already has its own chat sidebar, mobile app (React Native / Flutter / Swift / Kotlin), or custom avatar/UI, and you only want Ilubot's backend session, pedagogy, visemes, and configuration API.

#### 1. REST Endpoints (`core/conference_server.py`)

| Method & Path | Purpose | Request Body | Response |
|---|---|---|---|
| `GET /` | Serves the built-in conference UI (`web/index.html`) | — | HTML |
| `GET /api/teaching_language` | Get active language & supported list | — | `{"language": "English", "options": [...]}` |
| `POST /api/teaching_language` | Set teaching language (applied on next `start_call`) | `{"language": "Spanish"}` | `{"language": "Spanish"}` |
| `GET /api/stage_style` | Get stage visual mode | — | `{"style": "brain" \| "ilubot" \| "avatar"}` |
| `POST /api/stage_style` | Persist stage visual mode | `{"style": "brain"}` | `{"style": "brain"}` |
| `GET /avatar/{name}` | Fetch 3D `.glb` avatar (`ZainAI.glb`, `lipsync_eyes.glb`, etc.) | — | `model/gltf-binary` |
| `GET /web/{file}` | Fetch bundled Three.js / loader assets | — | Static JS |

#### 2. WebSocket Protocol (`ws(s)://<host>:<port>/ws`)

**Client → Server Messages (JSON):**

```jsonc
// 1. Start (or restart) a tutoring call — applies current teaching_language
{ "action": "start_call" }   // ("wake" is also accepted)

// 2. Send a student question or study-material context prompt
{ "action": "command", "text": "Why does mitosis produce two identical cells?" }

// 3. Interrupt current speech / reset turn state to LISTENING
{ "action": "interrupt" }

// 4. Sync microphone mute state
{ "action": "toggle_mute", "muted": true }

// 5. End the tutoring call (enters SLEEPING state)
{ "action": "leave" }

// 6. Optional callbacks wired in Desktop mode (main.py / ui.py):
{ "action": "voice_changed", "voice": "Charon" }
{ "action": "screen_capture" }
```

**Server → Client Events (JSON):**

| `type` | Payload Example | Description |
|---|---|---|
| `state` | `{"type":"state","state":"LISTENING"\|"THINKING"\|"SPEAKING"\|"SLEEPING"}` | Tutor lifecycle state |
| `content` | `{"type":"content","title":"Ilubot","text":"..."}` | Tutor's spoken/text response |
| `viseme` | `{"type":"viseme","level":0.72,"openness":0.65,"width":0.41,"tag":"AA"}` | Real-time lip-sync frame (`AA`, `E`, `I`, `O`, `U`, `MBP`, `FV`, `S`, `TD`, `K`, `CH`, `R`, `L`, `REST`) |
| `audio_level` | `{"type":"audio_level","level":0.45}` | Voice amplitude for waveforms/HUDs |
| `log` | `{"type":"log","message":"SYS: Call connected..."}` | System diagnostic messages |
| `mute` | `{"type":"mute","muted":true}` | Confirmed mute state |

#### 3. Minimal JavaScript / TypeScript Client Example

```js
class IlubotClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.wsUrl = this.baseUrl.replace(/^http/, "ws") + "/ws";
    this.ws = null;
  }

  async setLanguage(language) {
    await fetch(`${this.baseUrl}/api/teaching_language`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
    });
  }

  connect({ onReply, onState, onViseme, onOpen }) {
    this.ws = new WebSocket(this.wsUrl);
    this.ws.onopen = () => onOpen?.();
    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "content") onReply?.(msg.text, msg.title);
      else if (msg.type === "state") onState?.(msg.state);
      else if (msg.type === "viseme") onViseme?.(msg);
    };
  }

  startCall() {
    this.ws?.send(JSON.stringify({ action: "start_call" }));
  }

  askWithStudyMaterial({ lessonTitle, excerpt, studentQuestion }) {
    const prompt = [
      `[STUDY_MATERIAL title="${lessonTitle}"]`,
      excerpt,
      `[/STUDY_MATERIAL]`,
      ``,
      `Student question: ${studentQuestion}`,
    ].join("\n");
    this.ws?.send(JSON.stringify({ action: "command", text: prompt }));
  }

  interrupt() {
    this.ws?.send(JSON.stringify({ action: "interrupt" }));
  }

  endCall() {
    this.ws?.send(JSON.stringify({ action: "leave" }));
  }
}
```

---

### Pattern C: Desktop Companion Mode (Third-Party Study Apps)

When you want students to use Ilubot alongside **any** existing desktop/web study platform without modifying that platform's code (PDF viewers, IDE coding exercises, Coursera/edX, slides, or physical paper homework):

1. Configure and launch the full desktop assistant (`python main.py`).
2. `RESTRICTED_MODE = True` in `main.py` keeps the tutor locked to `screen_process`, `close_camera`, and `shutdown_jarvis`.
3. `actions/screen_processor.py` captures the student's active monitor (`_capture_screen()`) or webcam (`_capture_camera()`) as compressed `1280×720` JPEG frames and injects them straight into the Gemini Live session.
4. `ui.py` embeds `http://127.0.0.1:8085/` in a compact portrait window alongside the student's study window.

*(Optional)* Desktop mode also runs `dashboard/server.py` (`HTTP :8000` redirecting to `HTTPS :8001`) for paired mobile control over `/api/command`, `/api/upload` (send study PDFs/images directly from a phone), `/ws`, and `/ws/phone-audio` using PBKDF2 + AES-256-GCM.

---

## 4. Passing Study Material Context from Your Application

Study-material apps typically provide **text** (lesson paragraphs, quiz prompts, transcripts, code exercises) and/or **visuals** (PDF pages, diagrams, slides, geometry problems, handwritten work).

### 4.1 Text & Structured Context (Works Out of the Box)

Whenever the student switches chapters, selects a paragraph, or clicks **"Explain this problem"** in your app:

1. Wrap the material in explicit data delimiters so user-authored or external study text cannot override Ilubot's system instructions (consistent with Ilubot's `[UNTRUSTED CONTENT]` pattern in `core/prompt.txt`):
   ```text
   <<<UNTRUSTED source=study_app title="Cell Respiration - Ch 7" page="14">>>
   Glycolysis takes place in the cytoplasm and breaks one glucose molecule into two pyruvate molecules...
   <<<END UNTRUSTED>>>

   Student question: Why doesn't glycolysis require oxygen?
   ```
2. Send it via `{"action": "command", "text": "..."}` over `/ws`.
3. Because `HeadlessSession` preserves conversation state inside `self.chat`, you only need to resend the study excerpt when the student navigates to a **new** section or exercise.

---

### 4.2 Extending Ilubot for Multimodal Study Captures (PDF Canvas / Slides / Screenshots)

> **Current Behavior Note**: In desktop mode (`main.py`), `screen_process` captures the local computer screen or webcam via `mss`/`cv2`. In headless Cloud Run/Docker mode (`headless_server.py`), the server has no access to the student's browser screen, and `HeadlessSession` currently wires `on_text_command`, `on_start_call`, `on_leave_call`, `on_interrupt`, and `on_mute_toggled` (text only; `on_screen_capture` is not yet wired in `headless_server.py`).

To let a web-based study app send the **active slide, PDF canvas, diagram, or webcam snapshot** directly to `HeadlessSession` for multimodal Socratic tutoring, add the following extension:

#### Step 1 — Add `on_study_payload` in `core/conference_server.py`
Inside `ConferenceServer.__init__`:
```python
self.on_study_payload: Callable[[dict[str, Any]], None] | None = None
```
Inside `websocket_endpoint` in `core/conference_server.py`:
```python
elif action == "study_payload" and self.on_study_payload:
    self.on_study_payload(data)
```

#### Step 2 — Handle Multimodal Image + Text in `headless_server.py`
Inside `HeadlessSession._wire_callbacks`:
```python
self.server.on_study_payload = self._on_study_payload
```
Add the handler method to `HeadlessSession`:
```python
def _on_study_payload(self, data: dict) -> None:
    import base64
    import threading
    from google.genai import types

    question = (data.get("text") or "Help me understand what is on this study page.").strip()
    context = (data.get("context") or "").strip()
    image_b64 = (data.get("image_base64") or "").strip()
    mime_type = (data.get("mime_type") or "image/jpeg").strip()

    if not self.client or not self.chat:
        self.api_key = get_api_key()
        self._init_gemini()
    if not self.client:
        self.server.broadcast_content("System", "GEMINI_API_KEY not configured.")
        return

    parts = []
    if context:
        safe_ctx = context.replace("<<<END UNTRUSTED>>>", "[END]")
        parts.append(f"<<<UNTRUSTED source=study_app>>>\n{safe_ctx}\n<<<END UNTRUSTED>>>")
    if image_b64:
        raw_b64 = image_b64.split(",", 1)[-1]
        parts.append(types.Part.from_bytes(data=base64.b64decode(raw_b64), mime_type=mime_type))
    parts.append(question)

    def _generate():
        try:
            self.server.broadcast_state("THINKING")
            response = self.chat.send_message(parts)
            reply = response.text or "Let's look at this step by step."
            self.server.broadcast_content("Ilubot", reply)
            self.server.broadcast_state("SPEAKING")
            self._simulate_visemes(duration=min(5.0, max(1.5, len(reply.split()) * 0.28)))
        except Exception as e:
            logger.error(f"Error querying Gemini with study payload: {e}")
            self.server.broadcast_content("Error", f"Could not generate response: {e}")
            self.server.broadcast_state("LISTENING")

    threading.Thread(target=_generate, daemon=True).start()
```

#### Step 3 — Capture a PDF Canvas / Study Viewport in Your Frontend
In a PDF.js / HTML5 canvas study app (or using `html2canvas` on a lesson container):
```js
function sendCurrentStudyPage(canvasEl, studentQuestion, lessonText = "") {
  const imageBase64 = canvasEl.toDataURL("image/jpeg", 0.82);
  ws.send(JSON.stringify({
    action: "study_payload",
    text: studentQuestion,
    context: lessonText,
    image_base64: imageBase64,
    mime_type: "image/jpeg"
  }));
}
```

---

### 4.3 Optional `postMessage` Bridge for `<iframe>` Study Apps

If you embed `web/index.html` in an `<iframe>` and want buttons in the parent study app (e.g., **"Ask Ilubot about this slide"** or **"Switch Language"**) to control the embedded Ilubot panel, add this listener at the bottom of `web/index.html`:

```js
window.addEventListener("message", (event) => {
  // Restrict to your study app origin in production:
  // if (event.origin !== "https://your-study-app.com") return;
  const msg = event.data;
  if (!msg || typeof msg !== "object") return;

  if (msg.type === "ilubot:ask" && msg.text) {
    if (callState !== "connected") initiateCall();
    sendChatMessage(msg.text);
  } else if (msg.type === "ilubot:language" && msg.language) {
    fetch("/api/teaching_language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: msg.language }),
    });
  }
});
```

Then from your host study application:
```js
const frame = document.getElementById("ilubot-frame").contentWindow;
frame.postMessage(
  {
    type: "ilubot:ask",
    text: "[STUDY_MATERIAL] Quadratic formula: x = (-b ± √(b² - 4ac)) / 2a [/STUDY_MATERIAL]\nI don't understand what the discriminant tells me."
  },
  "https://ilubot.yourdomain.com"
);
```

---

## 5. Production Hardening Checklist (Multi-Student Platforms)

Before rolling Ilubot out to multiple concurrent learners in an EdTech production environment, address these four architectural items in the current codebase:

1. **Per-Student Session Isolation (`headless_server.py` & `core/conference_server.py`)**
   - *Current state*: `headless_server.py` creates one global `HeadlessSession` and `ConferenceServer.broadcast()` sends messages to all sockets in `self.active_websockets`. Suitable for single-user containers, demos, or local embeds.
   - *For multi-user SaaS*: Key sessions by WebSocket / signed `session_id` (e.g., `dict[str, HeadlessSession]`) so each student has an isolated Gemini `chat` history and receives only their own `content`, `state`, and `viseme` events.
2. **Authentication & Authorization**
   - `ConferenceServer` currently leaves `/ws` and `/api/*` open without auth. Add a short-lived signed JWT or session token check on `/ws?token=...` and `/api/*` (see `dashboard/server.py` for an existing token-validation example).
3. **CORS Origin Allow-Listing**
   - `core/conference_server.py` currently sets `allow_origins=["*"]`. Restrict `allow_origins` to your study application's origin(s) in production.
4. **Curriculum & Data-Privacy Customization**
   - **Pedagogy customization**: Adjust the system instruction in `headless_server.py` (lines 74–95) or `core/prompt.txt` to include grade level, subject rubric, or curriculum standards while keeping the 3–4 turn Socratic discovery loop.
   - **Self-hosted / Offline inference**: For strict institutional privacy requirements, desktop/local deployments support swapping Gemini for Ollama, LM Studio, vLLM, or OpenAI-compatible endpoints plus local Whisper STT and Piper TTS via `providers/`.

---

## 6. Verification Checklist

1. **Health & UI Check**:
   ```bash
   curl -i http://localhost:8080/
   curl http://localhost:8080/api/teaching_language
   ```
   Expected: `200 OK` serving `web/index.html` and JSON containing `"language"` and `"options"`.

2. **Language Persistence Check**:
   ```bash
   curl -X POST http://localhost:8080/api/teaching_language \
     -H "Content-Type: application/json" \
     -d '{"language":"Arabic"}'
   ```
   Expected: `{"language": "Arabic"}`.

3. **WebSocket Tutoring Flow Smoke Test**:
   Connect to `ws://localhost:8080/ws`, send `{"action":"start_call"}`, verify `state` transitions (`LISTENING` → `SPEAKING` → `LISTENING`) and greeting `content` arrives, then send:
   ```json
   {"action":"command","text":"[STUDY_MATERIAL] Photosynthesis converts CO2 and water into glucose using light. [/STUDY_MATERIAL]\nStudent question: Where does the oxygen released come from?"}
   ```
   Expected: Ilubot responds with a concise **Turn 1 guiding question** rather than immediately giving the direct answer, and confirms the answer by Turn 3–4.
