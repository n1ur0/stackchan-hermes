"""Voice-turn bridge: device-driven capture → STT → Hermes Agent → TTS.

yorishiro fork specific module (not intended for upstream PR).

The firmware records while the user holds a device-side trigger (LCD
tap) and the gateway's :mod:`audio_input_hook` POSTs the finished
capture as Ogg/Opus to ``STACKCHAN_AUDIO_HOOK_URL``. Pointing that URL
at this gateway's own capture server (``http://127.0.0.1:8766/voice_turn``)
closes the conversation loop in-process:

    tap → record → POST /voice_turn → STT (faster-whisper)
        → Hermes Agent (OpenAI-compatible API server)
        → TTS (say() pipeline) → device speaker

Environment variables:

- ``HERMES_API_URL`` — base URL of the Hermes API server adapter.
  Defaults to ``http://127.0.0.1:8642`` (the stackchan profile gateway).
- ``HERMES_API_KEY`` — bearer token for the Hermes API server. Optional;
  when set, voice turns use the native Sessions API
  (``POST /api/sessions`` + ``/api/sessions/{id}/chat/stream``), which
  owns conversation history server-side — multi-turn context works
  without any client-resent history or ``X-Hermes-Session-Id`` header.
- ``HERMES_SESSION_ID`` — Phase 2 (legacy): the *base namespace* formerly
  used to mint client-side session ids. The native Sessions API replaced
  that with real server sessions; the id is now only a fixed fallback
  when ``HERMES_SESSION_WINDOW_S=0``. Defaults to ``stackchan-voice``.
- ``HERMES_VOICE_SYSTEM_PROMPT`` — overrides the default system prompt
  that keeps spoken replies short.
- ``STACKCHAN_AUDIO_HOOK_TOKEN`` — shared bearer token; when set, the
  ``/voice_turn`` endpoint rejects requests without it (the sender side
  in :mod:`audio_input_hook` attaches the same token).
- ``STACKCHAN_LOCAL_LLM_MODEL`` (and friends) — opt-in fast path that
  routes short/simple utterances to a local Ollama model instead of
  Hermes; see :mod:`stackchan_mcp.local_llm`. Unset = every turn goes
  to Hermes, exactly as before.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import http, local_llm, multiturn

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

DEFAULT_HERMES_API_URL = "http://127.0.0.1:8642"
DEFAULT_HERMES_SESSION_ID = "stackchan-voice"

#: Whisper (and whisper.cpp) tag non-speech audio with bracketed labels
#: in the recognition language — "[MÚSICA]", "[Som de futebol]",
#: "[Applause]", "[no speech]" etc. Ambient TV/room noise produces
#: exactly this shape (often hallucinated onto silence), and it must
#: never reach Hermes as a user turn: the bot would answer the TV
#: instead of the person. A transcript consisting *only* of bracket
#: labels is treated like an empty transcript (drop the turn).
_NON_SPEECH_LABEL_RE = re.compile(r"^(?:\[[^\]\n]*\][\s]*)+$")


#: Hermes/DeepSeek drifts chatty even with the short-answer system prompt,
#: and the device streams audio in realtime (one 60 ms Opus frame per
#: push into a ~2.4 s decode queue), so a long reply stretches the turn
#: wall-clock linerally AND lets rapid taps queue behind the tts_lock.
#: A deterministic sentence/char budget keeps every spoken reply ≤ ~5 s.
def _estimate_speech_duration_ms(reply: str) -> int:
    """Rough spoken length of a (clamped) reply, in ms.

    Used only to size the talking choreography (mouth sequence + speech
    nods) that plays *while* the TTS audio is pushed. It need not be
    frame-accurate: the mouth sequence just has to read as "speaking".
    ~90 ms per character approximates a calm English reading pace with
    a short floor so even a one-word reply gets a beat of lip-sync.
    """
    n = len((reply or "").strip())
    if n <= 0:
        return 0
    return max(1200, int(n * 90))


#: Env-tunable: STACKCHAN_MAX_REPLY_CHARS (default 160),
#: STACKCHAN_MAX_REPLY_SENTENCES (default 2).
def _clamp_reply_for_voice(reply: str) -> str:
    reply = (reply or "").strip()
    if not reply:
        return reply
    max_chars = int(os.getenv("STACKCHAN_MAX_REPLY_CHARS", "160"))
    max_sentences = int(os.getenv("STACKCHAN_MAX_REPLY_SENTENCES", "2"))
    if len(reply) <= max_chars:
        return reply
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?…])\s+", reply):
        if (
            len(kept) >= max_sentences
            or sum(map(len, kept)) + len(sentence) > max_chars
        ):
            break
        kept.append(sentence)
    clamped = " ".join(kept).strip()
    return clamped or reply[:max_chars].rstrip()


#: Prompt for spoken replies that summarise rather than dump. StackChan is a
#: small voice robot: it should read/search, then give the user a concise but
#: complete digest — the key point first, then the essentials — instead of
#: reading back the raw retrieved content verbatim (which becomes a wall of
#: speech on the 1 W speaker). "Summarise" means condense to what matters,
#: not truncate mid-thought: a well-formed digest has an ending. It should be
#: a thorough spoken briefing, not a one-liner: mention EVERY distinct story
#: or point the sources carry, each with a concrete detail.
DEFAULT_VOICE_SYSTEM_PROMPT = (
    "You are StackChan, a small robot talking by voice. "
    "The user's speech comes from speech recognition, so fill in slight "
    "misrecognitions from context. Answer in natural, spoken language, "
    "as a clear spoken briefing. "
    "When you fetch or search for content, do not read it back verbatim — "
    "synthesize it, but be thorough: lead with the single most important "
    "point, then work through EVERY distinct story, headline or key fact the "
    "material covers, giving each its own complete sentence with concrete "
    "detail (who, what, numbers). Aim for a rich, informative briefing that "
    "covers the whole picture — usually six to ten sentences. Only add a "
    "tightener if you have extra room; never leave a major story out. "
    "Stay faithful to the source: don't invent details. Always finish with "
    "a complete sentence; don't just stop. No symbols, no bullet lists, "
    "no headings."
)

#: Tool-routing guidance appended to the voice system prompt. The
#: Hermes agent also has built-in tools (terminal, ...) that are
#: approval-gated in this deployment and tempt the model into dead
#: ends or fake completions — observed live in the Phase D2 E2E: a
#: weather question went to `curl wttr.in` via the terminal tool
#: (blocked pending approval) instead of the MCP web_search tool, and
#: a memo request was reported "added" without any tool call at all.
HERMES_VOICE_TOOLS_LINE = (
    "For research, weather, or news, always use the web_search MCP tool. "
    "Save memos and lists with write_note (append=true to add), check "
    "content with list_notes / read_note, control appliances with "
    "switchbot_* tools. Do not use other means such as terminal. "
    "Reporting 'done' or 'checked' without calling a tool is forbidden."
)

#: Hard ceiling for one Hermes turn. The agent may run tools internally;
#: beyond this the voice interaction is dead anyway.
HERMES_TIMEOUT_S = 120.0

#: Tool-name → short LCD label shown while the Hermes agent runs the
#: tool (hermes.tool.progress SSE events). The device status line is one
#: short line near the top of the LCD; labels must be terse and end with
#: "..." so the user knows a turn is still alive.
TOOL_STATUS_LABELS: dict[str, str] = {
    "web_search": "Searching...",
    "search_web": "Searching...",
    "write_note": "Saving note...",
    "read_note": "Reading note...",
    "list_notes": "Reading notes...",
    "take_photo": "Taking photo...",
    "move_head": "Moving head...",
    "set_avatar": "Changing face...",
    "switchbot_on": "Switching on...",
    "switchbot_off": "Switching off...",
    "switchbot_toggle": "Switching...",
    "get_status": "Checking status...",
    "get_device_status": "Checking status...",
}
DEFAULT_TOOL_STATUS = "Working..."

#: The web_search tool label carries the query itself; use it verbatim
#: (truncated) instead of the generic "Searching..." so the user sees
#: what the agent is looking up.
_SEARCH_LABEL_PREFIX = "Searching: "


def tool_status_text(tool: str, label: str = "") -> str:
    """Map a Hermes tool name to a short device status line."""
    base = TOOL_STATUS_LABELS.get(tool, DEFAULT_TOOL_STATUS)
    if tool == "web_search" and label.strip():
        text = _SEARCH_LABEL_PREFIX + label.strip()
        # LCD line is short; keep the query recognizable but bounded.
        if len(text) > 40:
            text = text[:39].rstrip() + "..."
        return text
    return base


#: Upper bound for one uploaded capture. Device-driven recordings are
#: capped at 30 s on the firmware side; Opus at 16 kHz mono runs well
#: under 4 KiB/s, so 2 MiB is an order of magnitude above any real
#: voice turn. The capture server disables aiohttp's global body cap
#: (``client_max_size=0`` for /pcm streaming), so this route enforces
#: its own.
MAX_OGG_BYTES = 2 * 1024 * 1024

#: Ogg/Opus → 16 kHz PCM decoding lives in :mod:`stackchan_mcp.http`;
#: this alias keeps the module-global patchable for tests/voice-turn.
_ogg_opus_to_pcm16k = http.ogg_opus_to_pcm16k


async def ask_hermes(
    text: str,
    *,
    session_id: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """Send one user turn to the Hermes API server, return the reply text.

    ``session_id`` is the per-conversation Hermes context id computed by
    the voice turn (Phase 2). When ``None`` (other callers, tests) it
    falls back to the fixed ``HERMES_SESSION_ID``, preserving the old
    behaviour.

    ``system_prompt`` overrides the spoken-reply prompt for callers with a
    different framing — the proactive speaker passes its own prompt so a
    state-transition utterance reads as a one-line greeting, not a chat
    answer. When ``None`` the usual ``HERMES_VOICE_SYSTEM_PROMPT`` env /
    default applies. The tool-routing line is always appended either way.
    """
    base_url = os.getenv("HERMES_API_URL", DEFAULT_HERMES_API_URL).rstrip("/")
    api_key = os.getenv("HERMES_API_KEY", "")
    if system_prompt is None:
        system_prompt = os.getenv(
            "HERMES_VOICE_SYSTEM_PROMPT", DEFAULT_VOICE_SYSTEM_PROMPT
        )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        # Session continuity is gated on API-key auth by the Hermes API
        # server; without the key we stay stateless.
        headers["X-Hermes-Session-Id"] = session_id or os.getenv(
            "HERMES_SESSION_ID", DEFAULT_HERMES_SESSION_ID
        )

    payload = {
        "model": "hermes-agent",
        "messages": [
            {
                "role": "system",
                "content": system_prompt + HERMES_VOICE_TOOLS_LINE,
            },
            {"role": "user", "content": text},
        ],
    }

    data = await http.post_json(
        f"{base_url}/v1/chat/completions",
        payload,
        name="Hermes API",
        timeout_s=HERMES_TIMEOUT_S,
        headers=headers,
    )
    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("Hermes API response missing choices: %s", str(data)[:500])
        raise RuntimeError("Hermes API response missing choices") from exc
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("Hermes API returned an empty reply")
    return reply.strip()


async def create_hermes_session() -> str:
    """Create an empty Hermes session via the native Sessions API.

    Returns the server-side session id (e.g. ``api_...``). The
    /api/sessions/{id}/chat/stream endpoint only accepts sessions that
    exist, so one POST per conversation (or rotation) is required — this
    IS the mint behind ``MultiturnSession.conversation_id``; no
    client-minted ``X-Hermes-Session-Id`` header is involved anymore.
    Title-less creation is fine (the server leaves ``title`` null);
    a unique title is only needed when the caller wants named sessions.

    Raises ``RuntimeError`` on any non-200 or a malformed body.
    """
    base_url = os.getenv("HERMES_API_URL", DEFAULT_HERMES_API_URL).rstrip("/")
    api_key = os.getenv("HERMES_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = await http.post_json(
        f"{base_url}/api/sessions",
        {},
        name="Hermes API (create session)",
        timeout_s=30.0,
        headers=headers,
    )
    session = data.get("session") or {}
    session_id = session.get("id")
    if not session_id:
        raise RuntimeError("Hermes API create-session response missing session.id")
    return session_id


async def ask_hermes_stream(
    text: str,
    *,
    session_id: str | None = None,
    system_prompt: str | None = None,
    on_step: Any | None = None,
) -> str:
    """Stream one user turn through the native Hermes Sessions API.

    POSTs to ``/api/sessions/{session_id}/chat/stream`` (the server owns
    conversation history, so multi-turn context survives client-side
    restarts — no ``X-Hermes-Session-Id`` header, no resending the
    history). The stream's SSE events carry live agent activity:

    - ``tool.started``: the agent began a tool — ``on_step(tool_name,
      preview)`` is awaited so the device can show progress like
      "Searching: ..." while the turn runs (Phase F).
    - ``assistant.delta``: reply text chunks, accumulated.
    - ``assistant.completed``: the authoritative final reply.
    - ``run.completed`` / ``run.failed`` / ``done``: terminal events.

    Returns the final reply text — or raises ``RuntimeError`` if the
    stream never produced a reply.
    """
    base_url = os.getenv("HERMES_API_URL", DEFAULT_HERMES_API_URL).rstrip("/")
    api_key = os.getenv("HERMES_API_KEY", "")
    if system_prompt is None:
        system_prompt = os.getenv(
            "HERMES_VOICE_SYSTEM_PROMPT", DEFAULT_VOICE_SYSTEM_PROMPT
        )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "input": text,
        "instructions": system_prompt + HERMES_VOICE_TOOLS_LINE,
    }

    reply_parts: list[str] = []
    final_content: str | None = None
    async for event, data in http.post_sse_events(
        f"{base_url}/api/sessions/{session_id}/chat/stream",
        payload,
        name="Hermes API",
        timeout_s=HERMES_TIMEOUT_S,
        headers=headers,
    ):
        if event == "tool.started" and on_step is not None:
            if isinstance(data, dict):
                try:
                    await on_step(data.get("tool_name", ""), data.get("preview", ""))
                except Exception:
                    logger.warning("ask_hermes_stream: on_step raised", exc_info=True)
            continue
        if event == "assistant.delta":
            delta = data.get("delta")
            if isinstance(delta, str):
                reply_parts.append(delta)
            continue
        if event == "assistant.completed":
            content = data.get("content")
            if isinstance(content, str) and content:
                final_content = content
            continue
        if event in ("run.completed", "run.failed", "run.cancelled", "done"):
            break

    reply = (final_content or "".join(reply_parts)).strip()
    if not reply:
        raise RuntimeError("Hermes API returned an empty reply")
    return reply


async def generate_reply(
    text: str,
    *,
    force_hermes: bool = False,
    session_id: str | None = None,
    on_step: Any | None = None,
) -> tuple[str, str]:
    """Produce the reply for one transcript, returning ``(reply, route)``.

    With local routing opted in (``STACKCHAN_LOCAL_LLM_MODEL`` set) and
    :func:`local_llm.decide_route` classifying the turn as short/simple,
    the local Ollama model answers; on any local failure (timeout,
    connection refused, bad response) the turn falls back to Hermes so
    routing can never kill a conversation. ``route`` is ``"local"`` or
    ``"hermes"``. With ``force_hermes`` set (the dashboard's Hermes-pin
    toggle) the local fast-path is skipped entirely and every turn goes
    to Hermes. ``session_id`` is the server-side Hermes session id
    (native Sessions API) threaded to :func:`ask_hermes_stream` so the
    Hermes-routed turn carries the per-conversation context (Phase 2).

    ``on_step`` is forwarded to :func:`ask_hermes_stream`: when the
    Hermes agent starts a tool, the callback receives ``(tool, label)``
    so the caller can show live progress on the device (Phase F). Local
    turns are too fast to need it.
    """
    if (
        not force_hermes
        and local_llm.is_enabled()
        and local_llm.decide_route(text) == local_llm.ROUTE_LOCAL
    ):
        system_prompt = os.getenv(
            "HERMES_VOICE_SYSTEM_PROMPT", DEFAULT_VOICE_SYSTEM_PROMPT
        )
        try:
            reply = await local_llm.ask_local(text, system_prompt=system_prompt)
            return reply, local_llm.ROUTE_LOCAL
        except Exception as exc:
            logger.warning(
                "voice_turn: local LLM failed (%s); falling back to Hermes", exc
            )
    return await ask_hermes_stream(
        text, session_id=session_id, on_step=on_step
    ), local_llm.ROUTE_HERMES


def _check_token(request: web.Request) -> bool:
    """Authorise a /voice_turn caller.

    With ``STACKCHAN_AUDIO_HOOK_TOKEN`` configured, require the matching
    bearer token. Without a token, fail closed for everything except
    loopback peers: the capture server binds non-loopback interfaces
    (the ESP32 POSTs /capture over the LAN), and this route invokes an
    agent — it must not be open to the whole LAN by default.
    """
    expected = os.getenv("STACKCHAN_AUDIO_HOOK_TOKEN", "")
    if not expected:
        return request.remote in ("127.0.0.1", "::1")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth[len("Bearer ") :], expected)


async def handle_voice_turn(request: web.Request) -> web.Response:
    """POST /voice_turn — run one full voice conversation turn."""
    # Lazy imports keep capture-only deployments free of the stt/tts
    # extras (same pattern as the /pcm handler).
    from . import control
    from .capture_server import GATEWAY_KEY
    from .stt import get_registry as get_stt_registry
    from .stt.orchestrator import DEFAULT_ENGINE as DEFAULT_STT_ENGINE
    from .tts.orchestrator import synthesize_and_send

    if not _check_token(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    gateway: "Gateway | None" = request.app.get(GATEWAY_KEY)  # type: ignore[assignment]
    if gateway is None:
        return web.json_response(
            {"ok": False, "error": "gateway not attached"}, status=503
        )

    # Phase E: stamp the interaction at the very start of the turn —
    # while the STT/Hermes round-trip is in flight neither tts_lock nor
    # the recording slot is held, so this timestamp is what keeps the
    # heartbeat from speaking into that gap.
    gateway.note_human_interaction()

    # Phase F: mark the turn in flight so device-side tools (web_search)
    # can show a "Searching..." status, and always clear the status text +
    # flag in the finally below so the display never gets stuck.
    gateway.voice_turn_active = True

    # Multi-turn (yorishiro fork): a new turn is starting, so close any
    # open continuation gap — this turn's processing is covered by
    # voice_turn_active. If the gap went stale (the user's answer took
    # too long, or this is a fresh tap minutes later), reset the counter
    # so it counts as a new conversation rather than turn N+1.
    now = time.monotonic()
    gateway.multiturn_active = False
    if gateway.multiturn.is_gap_stale(now, multiturn.session_timeout_s()):
        gateway.multiturn.reset()

    # Phase 2 — per-conversation Hermes context. Rotate to a fresh
    # server-side session (created via the native Sessions API) when the
    # previous turn is older than the context window (or none is open)
    # and reuse it across the turns of one conversation, so Hermes keeps
    # context without piling every conversation into one ever-growing
    # session. HERMES_SESSION_WINDOW_S=0 disables rotation (one
    # persistent session, as before). conversation_id reads
    # last_activity *before* we advance it for this turn. A session
    # creation failure here is not fatal: the turn proceeds with an
    # empty id and only an actual Hermes-routed call errors out (local
    # turns stay up when the Hermes box is down).
    try:
        hermes_session_id = await gateway.multiturn.conversation_id(
            now=now,
            window_s=multiturn.session_window_s(),
            mint=create_hermes_session,
        )
    except Exception as exc:
        logger.warning("voice_turn: Hermes session create failed: %s", exc)
        hermes_session_id = ""
    gateway.multiturn.last_activity = now

    session_id = request.headers.get("X-StackChan-Session", "")
    try:
        return await _run_voice_turn(
            request,
            gateway,
            session_id,
            hermes_session_id=hermes_session_id,
            get_stt_registry=get_stt_registry,
            default_stt_engine=DEFAULT_STT_ENGINE,
            synthesize_and_send=synthesize_and_send,
            control=control,
        )
    finally:
        # Phase F: always clear the on-device status text, subtitle,
        # route badge and indicator LED, and drop the in-flight flag,
        # no matter how the turn ended (success, early return, or
        # exception). Restoring the LED here (rather than a hard "off")
        # is what guarantees the gateway never leaves the response
        # indicator lit over the firmware's autonomous listening LED:
        # restore_idle_led re-lights the user's chosen idle colour, or
        # clears the LEDs when no idle colour is set (Phase 2).
        gateway.voice_turn_active = False
        # Multi-turn (yorishiro fork): when this turn just re-opened
        # listening for a follow-up, leave the display alone —
        # on_listen_started now owns the listening status/LED, and
        # clearing here would blank it and flicker the idle colour over
        # the firmware's listening state. The next turn (or a stale-gap
        # entry / non-continuing turn) restores the display normally.
        if not gateway.multiturn_active:
            await control.set_device_status_text(gateway, control.STATUS_CLEAR)
            await control.set_device_route_badge(gateway, "")
            await control.restore_idle_led(gateway)
            # Aliveness: turn over — cancel any talking/weave motion, restore
            # the home head pose, face back to idle with blink on.
            gateway.choreo.release()
            # Multi-turn UX: if this turn hit the conversation's turn
            # ceiling on a still-open question, leave a "tap to continue"
            # hint on screen instead of blanking the subtitle. The flag is
            # set in _maybe_continue and is one-shot (consumed here).
            if getattr(gateway, "multiturn_prompt_pending", False):
                await control.set_device_subtitle(
                    gateway, multiturn.TAP_TO_CONTINUE_HINT
                )
                gateway.multiturn_prompt_pending = False
            else:
                await control.set_device_subtitle(gateway, "")


async def _run_voice_turn(
    request: web.Request,
    gateway: "Gateway",
    session_id: str,
    *,
    hermes_session_id: str = "",
    get_stt_registry: Any,
    default_stt_engine: str,
    synthesize_and_send: Any,
    control: Any,
) -> web.Response:
    """Body of one voice turn; the caller owns status-text cleanup.

    ``session_id`` is the device WS session (``X-StackChan-Session``);
    ``hermes_session_id`` is the Phase 2 per-conversation Hermes context
    id threaded into the brain call.
    """
    if (request.content_length or 0) > MAX_OGG_BYTES:
        return web.json_response(
            {"ok": False, "error": "payload too large"}, status=413
        )
    # content_length can be absent/lied about (chunked transfer), so
    # also enforce the cap on the actual stream.
    ogg = await request.content.read(MAX_OGG_BYTES + 1)
    if len(ogg) > MAX_OGG_BYTES:
        return web.json_response(
            {"ok": False, "error": "payload too large"}, status=413
        )
    if not ogg:
        return web.json_response({"ok": False, "error": "empty body"}, status=400)

    # Opt-in capture dump for diagnosing mic quality (STT/VAD issues).
    dump_dir = os.getenv("STACKCHAN_VOICE_DUMP_DIR", "")
    if dump_dir:
        try:
            os.makedirs(dump_dir, exist_ok=True)
            dump_path = os.path.join(dump_dir, f"voice_turn_{int(time.time())}.ogg")
            with open(dump_path, "wb") as fp:
                fp.write(ogg)
            logger.info("voice_turn: capture dumped to %s", dump_path)
        except OSError:
            logger.exception("voice_turn: capture dump failed")

    t0 = time.monotonic()
    try:
        pcm = await asyncio.to_thread(_ogg_opus_to_pcm16k, ogg)
    except Exception as exc:
        logger.exception("voice_turn: Ogg/Opus decode failed")
        return web.json_response(
            {"ok": False, "error": f"decode failed: {exc}"}, status=400
        )
    t_decode = time.monotonic()

    engine = get_stt_registry().get(default_stt_engine)
    if engine is None:
        return web.json_response(
            {
                "ok": False,
                "error": (
                    f"STT engine '{default_stt_engine}' not registered — "
                    "install stackchan-mcp[stt-faster-whisper]"
                ),
            },
            status=503,
        )
    # Phase F: the capture already finished (audio arrives post-record),
    # so "I'm listening..." reads naturally at the start of recognition; flip
    # to "Thinking..." the moment STT is done and the brain takes over.
    await control.set_device_status_text(gateway, control.STATUS_LISTENING)
    # Aliveness: conversation just opened — welcome glance + idle face.
    gateway.choreo.engage()
    # Phase 2 LED: show the "listening" colour through STT (self-
    # contained; on_listen_started already set it for device listens).
    await control.apply_led_state(gateway, "listening")
    # yorishiro fork: the default recognition language is env-driven so
    # a PT/EN deployment does not need a code change (upstream default
    # stays "ja").
    stt_language = os.getenv("STACKCHAN_STT_LANGUAGE", "ja")
    stt_result: dict[str, Any] = await engine.transcribe(pcm, language=stt_language)
    transcript = stt_result.get("text", "").strip()
    t_stt = time.monotonic()

    if not transcript:
        logger.info("voice_turn: empty transcript (noise?), session=%s", session_id)
        # Multi-turn: silence ends the conversation — if this was the
        # auto-reopened listen after a Hermes question and the user said
        # nothing, drop the continuation counter so the loop stops here.
        gateway.multiturn.reset()
        return web.json_response(
            {"ok": False, "reason": "empty transcript", "session_id": session_id}
        )

    # Non-speech bracket labels ("[Som de futebol]", "[MÚSICA]") are
    # whisper's way of saying "no human speech here" — ambient TV noise
    # hallucinated into a label. Never feed those to Hermes: same drop
    # path as an empty transcript (reset multiturn, stay quiet).
    if _NON_SPEECH_LABEL_RE.match(transcript):
        logger.info(
            "voice_turn: non-speech labels (%r), dropping session=%s",
            transcript[:120],
            session_id,
        )
        gateway.multiturn.reset()
        return web.json_response(
            {"ok": False, "reason": "non-speech", "session_id": session_id}
        )

    logger.info("voice_turn: transcript=%r session=%s", transcript[:120], session_id)
    await control.set_device_status_text(gateway, control.STATUS_THINKING)
    # Aliveness: LLM working — pensive lateral weave behind "Thinking...".
    gateway.choreo.thinking()

    # Phase F (steps): stream the Hermes turn and surface each tool the
    # agent starts as a short LCD status line ("Searching...", "Saving
    # note...", ...) so the screen reflects actual progress instead of a
    # static "Thinking..." for the whole LLM round-trip. Best-effort:
    # set_device_status_text swallows failures.
    _first_tool_seen = False

    async def _report_step(tool: str, label: str = "") -> None:
        await control.set_device_status_text(gateway, tool_status_text(tool, label))
        # Aliveness: a light consult-tilt on the first tool only; later
        # tools keep motion muted (the status line is the signal).
        nonlocal _first_tool_seen
        gateway.choreo.tool_step(is_first=not _first_tool_seen)
        _first_tool_seen = True

    # The dashboard's Hermes-pin toggle (persisted in the control state):
    # read once per turn and thread into both the LED hint and the reply
    # routing so a pinned turn lights the Hermes colour immediately.
    force_hermes = control.routing_force_hermes()
    # Phase 2 LED: light the colour for whichever brain is about to run,
    # so it reads as "Hermes is thinking" in real time (same rule-based
    # classifier generate_reply uses). Local turns keep the listening
    # colour through their fast "preparing" phase.
    if (
        force_hermes
        or not local_llm.is_enabled()
        or local_llm.decide_route(transcript) == local_llm.ROUTE_HERMES
    ):
        await control.apply_led_state(gateway, "hermes")
    try:
        reply, route = await generate_reply(
            transcript,
            force_hermes=force_hermes,
            session_id=hermes_session_id,
            on_step=_report_step,
        )
    except Exception as exc:
        logger.exception("voice_turn: Hermes call failed")
        return web.json_response(
            {"ok": False, "error": f"hermes failed: {exc}", "transcript": transcript},
            status=502,
        )
    t_llm = time.monotonic()

    logger.info("voice_turn: reply=%r session=%s", reply[:120], session_id)
    # Keep the spoken turn short: see _clamp_reply_for_voice.
    reply = _clamp_reply_for_voice(reply)
    # Phase F: light the "H" badge + the Hermes LED colour for
    # Hermes-routed turns. Local-LLM turns stay badge-free and keep the
    # listening colour. Re-asserting the Hermes colour here (idempotent
    # with the pre-call set above) covers a local→Hermes fallback. The
    # outer handle_voice_turn finally restores the idle LED on every
    # exit path (incl. a TTS failure below).
    if route == local_llm.ROUTE_HERMES:
        await control.set_device_route_badge(gateway, "H")
        await control.apply_led_state(gateway, "hermes")
    # Aliveness: the reply is about to play, and synthesize_and_send pushes
    # audio at real-time pace (so it returns only after the reply has
    # played). Start the talking choreography — happy face, lip-sync mouth
    # sized to the reply, a few speech nods — concurrently so body language
    # tracks the voice instead of landing after it.
    gateway.choreo.talk(_estimate_speech_duration_ms(reply))
    try:
        tts_result = await synthesize_and_send(
            {"text": reply},
            gateway=gateway,
            # Show the spoken reply as the subtitle only once the audio is
            # actually loaded (PCM synthesised and about to be pushed), so
            # the text never sits on screen during the TTS synthesis gap.
            # The finally in handle_voice_turn still clears it afterwards.
            on_audio_ready=lambda: control.set_device_subtitle(gateway, reply),
        )
    except Exception as exc:
        logger.exception("voice_turn: TTS failed")
        return web.json_response(
            {
                "ok": False,
                "error": f"tts failed: {exc}",
                "transcript": transcript,
                "reply": reply,
            },
            status=502,
        )
    t_done = time.monotonic()

    timings_ms = {
        "decode": int((t_decode - t0) * 1000),
        "stt": int((t_stt - t_decode) * 1000),
        # "llm" covers whichever brain answered; "route" says which.
        "llm": int((t_llm - t_stt) * 1000),
        "tts": int((t_done - t_llm) * 1000),
        "total": int((t_done - t0) * 1000),
    }
    logger.info(
        "voice_turn: done session=%s route=%s timings_ms=%s",
        session_id,
        route,
        timings_ms,
    )
    # Phase F: record this completed round-trip (transcript + spoken
    # reply) into the rolling conversation log for GET
    # /control/conversation. Only turns that made it past STT, Hermes
    # and TTS reach here — empty transcripts and Hermes/TTS failures
    # return earlier and are intentionally not logged.
    control.record_conversation_turn(transcript, reply, route, timings_ms)
    # Multi-turn (yorishiro fork): if Hermes ended this turn with a
    # question, re-open listening so the user can answer hands-free.
    # Bounded + opt-in; resets the counter when the conversation ends.
    multiturn_continued = await _maybe_continue(gateway, reply, route, control)
    return web.json_response(
        {
            "ok": True,
            "session_id": session_id,
            "transcript": transcript,
            "reply": reply,
            "route": route,
            "tts": tts_result,
            "timings_ms": timings_ms,
            "multiturn": multiturn_continued,
        }
    )


async def _maybe_continue(
    gateway: "Gateway",
    reply: str,
    route: str,
    control: Any,
) -> bool:
    """Re-open listening after a turn iff Hermes invited a follow-up.

    Returns True when a continuation listen was fired. On any
    non-continuing turn the per-conversation counter is reset so the
    next tap starts a brand-new conversation. See
    :mod:`stackchan_mcp.multiturn` and CLAUDE.md design principle #1.
    """
    from .audio_stream import is_recording

    # Master gate first — the persisted dashboard toggle (its default is
    # seeded from the legacy STACKCHAN_MULTITURN env, but the toggle wins).
    # When the feature is disabled the conversation always ends after one
    # round-trip, so a normal turn costs nothing extra.
    if not control.multiturn_enabled():
        gateway.multiturn.reset()
        return False

    cont = multiturn.should_continue(
        enabled=True,
        route=route,
        reply=reply,
        turn_count=gateway.multiturn.turn_count,
        max_turns=multiturn.max_turns(),
        device_connected=gateway.esp32.device_connected,
        muted=control.is_muted(),
        recording=is_recording(),
    )
    if not cont:
        # The conversation is over — clear the counter. UX: when Hermes
        # still invited a follow-up but we stopped *only* because the
        # per-conversation turn ceiling was reached, flag a gentle "tap to
        # continue" subtitle so the device doesn't fall silent mid-question
        # (handle_voice_turn's finally consumes the flag). Other stop
        # reasons — no question, local route, muted, disconnected — end
        # silently as before.
        if (
            route == "hermes"
            and multiturn.reply_invites_continuation(reply)
            and gateway.multiturn.turn_count >= multiturn.max_turns()
            and gateway.esp32.device_connected
            and not control.is_muted()
            and not is_recording()
        ):
            gateway.multiturn_prompt_pending = True
        gateway.multiturn.reset()
        return False

    # Mark the gap active *before* the guard sleep so a heartbeat tick
    # during the wait is suppressed, then re-open listening once the
    # firmware decode-queue tail has drained. Frames are paced at real
    # time (tts/orchestrator.py), so only the ~0.8 s queue tail remains
    # after synthesize_and_send returns; AEC is off, so re-opening too
    # early risks the device hearing its own tail (tune the guard on
    # hardware via MULTITURN_TTS_GUARD_MS).
    gateway.multiturn.note_continuation(time.monotonic())
    gateway.multiturn_active = True
    guard_ms = multiturn.tts_guard_ms()
    if guard_ms:
        await asyncio.sleep(guard_ms / 1000.0)
    try:
        await gateway.esp32.send_listen_state("start", mode="manual")
    except ConnectionError:
        logger.warning("multiturn: device gone before re-listen; ending conversation")
        gateway.multiturn.reset()
        gateway.multiturn_active = False
        return False
    logger.info(
        "multiturn: re-opened listening (turn %d/%d)",
        gateway.multiturn.turn_count,
        multiturn.max_turns(),
    )
    return True
