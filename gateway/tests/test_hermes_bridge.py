"""Tests for the Hermes voice bridge (ask_hermes request shape)."""

import json
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from stackchan_mcp import control, hermes_bridge, local_llm, multiturn
from stackchan_mcp.capture_server import GATEWAY_KEY
from stackchan_mcp.hermes_bridge import (
    DEFAULT_VOICE_SYSTEM_PROMPT,
    HERMES_VOICE_TOOLS_LINE,
    ask_hermes,
)
from stackchan_mcp.multiturn import MultiturnSession


@pytest.fixture
def aiohttp_unused_port():
    """Helper: pick an unused TCP port via ephemeral bind."""
    import socket

    def _pick() -> int:
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        finally:
            sock.close()

    return _pick


async def _run_hermes_stub(handler, aiohttp_unused_port):
    """Run ``handler`` behind POST /v1/chat/completions; return (runner, base_url)."""
    app = web.Application()
    app.router.add_route("POST", "/v1/chat/completions", handler)
    port = aiohttp_unused_port()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, f"http://127.0.0.1:{port}"


@pytest.fixture
async def hermes_stub(monkeypatch, aiohttp_unused_port):
    """Stub Hermes' /v1/chat/completions with HERMES_API_URL already wired.

    Yields ``(received, coop)``: ``received`` captures the payload/headers
    seen by the stub; ``coop["content"]`` sets the assistant reply and
    ``coop["status"]``/``coop["text"]`` make the stub answer with an HTTP
    error instead (default reply: "ok").
    """
    received: dict[str, Any] = {}
    coop: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        received["headers"] = dict(request.headers)
        if "status" in coop:
            return web.Response(status=coop["status"], text=coop.get("text", ""))
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": coop.get("content", "ok"),
                        }
                    }
                ]
            }
        )

    runner, base_url = await _run_hermes_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("HERMES_API_URL", base_url)
    try:
        yield received, coop
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("env_prompt", "explicit_prompt", "user_text", "content"),
    [
        # Default voice prompt baked in.
        (None, None, "make a memo", "yes "),
        # A custom HERMES_VOICE_SYSTEM_PROMPT replaces the default.
        ("custom.", None, "hello", "ok"),
        # An explicit system_prompt= (proactive speaker) beats the env one.
        ("env-prompt.", "proactive prompt.", "situation test", "welcome back"),
    ],
    ids=["default-voice-prompt", "custom-env-prompt", "explicit-overrides-env"],
)
@pytest.mark.asyncio
async def test_ask_hermes_system_prompt_composition(
    monkeypatch, hermes_stub, env_prompt, explicit_prompt, user_text, content
):
    """The system message always pairs the voice-style prompt with the MCP
    tool-routing guidance; an explicit system_prompt wins over the env one.
    (Without the tool line the agent drifts to its approval-gated built-in
    tools or fakes completions — observed live in the Phase D2 E2E.)"""
    received, coop = hermes_stub
    coop["content"] = content
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    if env_prompt is None:
        monkeypatch.delenv("HERMES_VOICE_SYSTEM_PROMPT", raising=False)
    else:
        monkeypatch.setenv("HERMES_VOICE_SYSTEM_PROMPT", env_prompt)

    kwargs: dict[str, str] = {}
    if explicit_prompt is not None:
        kwargs["system_prompt"] = explicit_prompt
    reply = await ask_hermes(user_text, **kwargs)

    # Prefix: explicit prompt > env prompt > baked-in default.
    assert reply == content.strip()  # trailing whitespace stripped
    system = received["payload"]["messages"][0]
    assert system["role"] == "system"
    expected_prefix = explicit_prompt or env_prompt or DEFAULT_VOICE_SYSTEM_PROMPT
    assert system["content"].startswith(expected_prefix)
    # When overridden, the env prompt must not leak into the message.
    if explicit_prompt is not None and env_prompt is not None:
        assert env_prompt not in system["content"]
    assert HERMES_VOICE_TOOLS_LINE in system["content"]
    assert received["payload"]["messages"][1] == {"role": "user", "content": user_text}


@pytest.mark.asyncio
async def test_ask_hermes_error_status_raises(monkeypatch, hermes_stub):
    """An upstream HTTP error surfaces as RuntimeError with the status."""
    received, coop = hermes_stub
    coop["status"] = 500
    coop["text"] = "boom"
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="status=500"):
        await ask_hermes("hello")


# ---- Phase 2: per-conversation Hermes session id ---------------------


@pytest.mark.parametrize(
    ("api_key", "session_arg", "env_session", "expected_header"),
    [
        # Per-conversation id wins; Bearer auth sent with the key.
        ("secret", "stackchan-voice-abc123", None, "stackchan-voice-abc123"),
        # Without a per-conversation id the fixed HERMES_SESSION_ID is used.
        ("secret", None, "fixed-id", "fixed-id"),
        # No key: session continuity gated off, nothing leaks.
        (None, "stackchan-voice-abc123", None, None),
    ],
    ids=["explicit-session-id", "env-fallback-session-id", "no-key-no-session-header"],
)
@pytest.mark.asyncio
async def test_ask_hermes_sends_conversation_session_id(
    monkeypatch,
    hermes_stub,
    api_key,
    session_arg,
    env_session,
    expected_header,
):
    """The per-conversation id is sent as X-Hermes-Session-Id (explicit arg
    wins, else HERMES_SESSION_ID) — gated on the API key, with Bearer auth."""
    received, coop = hermes_stub
    if api_key is None:
        monkeypatch.delenv("HERMES_API_KEY", raising=False)
    else:
        monkeypatch.setenv("HERMES_API_KEY", api_key)
    if env_session:
        monkeypatch.setenv("HERMES_SESSION_ID", env_session)

    kwargs: dict[str, str] = {}
    if session_arg is not None:
        kwargs["session_id"] = session_arg
    await ask_hermes("hey", **kwargs)

    headers = received["headers"]
    if expected_header is None:
        assert "X-Hermes-Session-Id" not in headers
    else:
        assert headers["X-Hermes-Session-Id"] == expected_header
    if api_key:
        assert headers["Authorization"] == f"Bearer {api_key}"


# ---- voice-turn scaffolding (stub gateway + full pipeline) -----------


class _StubESP32:
    def __init__(self) -> None:
        self.device_connected = True
        self.listen_calls: list[tuple[str, str]] = []

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_calls.append((state, mode))


class _StubGateway:
    def __init__(self) -> None:
        self.esp32 = _StubESP32()
        self.voice_turn_active = False
        self.multiturn = MultiturnSession()
        self.multiturn_active = False
        self.multiturn_prompt_pending = False
        self._interactions = 0

    def note_human_interaction(self) -> None:
        self._interactions += 1


class _StubEngine:
    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, pcm, language="ja"):
        return {"text": self._text}


def _make_voice_request(gateway) -> web.Request:
    from aiohttp import StreamReader

    app = web.Application()
    app[GATEWAY_KEY] = gateway
    # A real StreamReader so request.content.read() works; make_mocked_request
    # otherwise leaves content unset / as a bytes placeholder.
    body = b"oggdata"
    reader = StreamReader(protocol=mock.Mock(_reading_paused=False), limit=2**16)
    reader.feed_data(body)
    reader.feed_eof()
    # Authorise via the shared hook token (set by the tests) rather than
    # the loopback fallback, which depends on a transport peername that
    # make_mocked_request does not populate in this aiohttp version.
    return make_mocked_request(
        "POST",
        "/voice_turn",
        headers={
            "X-StackChan-Session": "sess-1",
            "Authorization": "Bearer turn-token",
            "Content-Length": str(len(body)),
        },
        payload=reader,
        app=app,
    )


def _patch_voice_pipeline(
    monkeypatch, *, transcript: str, reply: str = "yes", route: str = "hermes"
):
    """Stub decode / STT / brain / TTS so the turn runs without deps."""
    import stackchan_mcp.stt as stt_mod
    import stackchan_mcp.tts.orchestrator as tts_orch

    monkeypatch.setattr(hermes_bridge, "_ogg_opus_to_pcm16k", lambda data: b"\x00\x00")

    class _Registry:
        def get(self, name):
            return _StubEngine(transcript)

    monkeypatch.setattr(stt_mod, "get_registry", lambda: _Registry())

    async def fake_generate_reply(text, *, force_hermes=False, session_id=None):
        return reply, route

    monkeypatch.setattr(hermes_bridge, "generate_reply", fake_generate_reply)
    # The turn reads the persisted Hermes-pin flag; default it off so
    # tests never touch the real ~/.stackchan control state.
    monkeypatch.setattr(control, "routing_force_hermes", lambda: False)
    # The multi-turn gate likewise reads persisted state; bind it to the
    # env reader so tests gate purely via STACKCHAN_MULTITURN and never
    # depend on (or get perturbed by) a live dashboard toggle in the real
    # control state file (mirrors the routing_force_hermes stub above).
    monkeypatch.setattr(control, "multiturn_enabled", multiturn.is_enabled)

    async def fake_send(arguments, *, gateway=None, **kw):
        return {"frame_count": 1}

    monkeypatch.setattr(tts_orch, "synthesize_and_send", fake_send)


def _record_device_cosmetics(monkeypatch) -> dict[str, list]:
    """Record subtitle / route-badge / LED calls in invocation order."""
    rec: dict[str, list] = {"subtitle": [], "badge": [], "led": []}

    async def fake_subtitle(gateway, text):
        rec["subtitle"].append(text)

    async def fake_badge(gateway, text):
        rec["badge"].append(text)

    async def fake_led(gateway, slot):
        rec["led"].append(slot)

    monkeypatch.setattr(control, "set_device_subtitle", fake_subtitle)
    monkeypatch.setattr(control, "set_device_route_badge", fake_badge)
    # Phase 2: the voice turn drives the LED via apply_led_state(slot)
    # (listening / hermes / idle) rather than the old raw indicator.
    monkeypatch.setattr(control, "apply_led_state", fake_led)
    return rec


def _force_route_hint(monkeypatch, route: str) -> None:
    """Pin the pre-call LED route hint (decide_route) for a turn.

    The bridge lights the "hermes" LED before running the brain when the
    rule-based classifier says Hermes; pin it so the LED sequence is
    deterministic regardless of the local-LLM env.
    """
    monkeypatch.setattr(local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(local_llm, "decide_route", lambda _t: route)


def _record_status_text(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def fake_status(gateway, text):
        seen.append(text)

    monkeypatch.setattr(control, "set_device_status_text", fake_status)
    return seen


class _VoiceTurnRunner:
    """Configured stub for one voice turn (see the ``voice_turn`` fixture)."""

    def __init__(self, gateway: _StubGateway, seen: list[str], rec: dict[str, list]):
        self.gateway = gateway
        self.seen = seen
        self.rec = rec

    async def run(self) -> web.Response:
        return await hermes_bridge.handle_voice_turn(_make_voice_request(self.gateway))


@pytest.fixture
def voice_turn(monkeypatch):
    """One-call voice-turn harness: hook token env + stubbed pipeline.

    The factory wires the status/cosmetic recorders and stub brains/TTS
    around a fresh _StubGateway and returns a ``runner`` whose attributes
    expose the recorded calls; ``await runner.run()`` executes one turn.
    Tests override stubs (``generate_reply=``, boom fakes) or gateway state
    between the build call and run().
    """
    monkeypatch.setenv("STACKCHAN_AUDIO_HOOK_TOKEN", "turn-token")

    def _make(
        *,
        transcript: str = "hey",
        reply: str = "yes",
        route: str = "hermes",
        route_hint: str | None = None,
        generate_reply=None,
    ) -> _VoiceTurnRunner:
        if route_hint is not None:
            _force_route_hint(monkeypatch, route_hint)
        gateway = _StubGateway()
        seen = _record_status_text(monkeypatch)
        rec = _record_device_cosmetics(monkeypatch)
        _patch_voice_pipeline(
            monkeypatch, transcript=transcript, reply=reply, route=route
        )
        if generate_reply is not None:
            monkeypatch.setattr(hermes_bridge, "generate_reply", generate_reply)
        return _VoiceTurnRunner(gateway, seen, rec)

    return _make


# ---- Phase F: voice-turn status-text feedback ------------------------


@pytest.mark.asyncio
async def test_voice_turn_status_text_sequence(monkeypatch, voice_turn):
    runner = voice_turn(transcript="good morning", reply="hey")
    response = await runner.run()

    assert response.status == 200
    # I'm listening... (STT) → Thinking... (brain) → "" (clear in finally).
    assert runner.seen == [
        control.STATUS_LISTENING,
        control.STATUS_THINKING,
        control.STATUS_CLEAR,
    ]
    assert runner.gateway.voice_turn_active is False
    assert runner.gateway._interactions == 1


@pytest.mark.asyncio
async def test_voice_turn_clears_status_on_empty_transcript(monkeypatch, voice_turn):
    runner = voice_turn(transcript="   ")
    response = await runner.run()

    assert response.status == 200
    # Listening shown, then cleared in finally (no Thinking... — empty STT).
    assert runner.seen[0] == control.STATUS_LISTENING
    assert runner.seen[-1] == control.STATUS_CLEAR
    assert control.STATUS_THINKING not in runner.seen
    assert runner.gateway.voice_turn_active is False


@pytest.mark.asyncio
async def test_voice_turn_clears_status_when_brain_fails(monkeypatch, voice_turn):
    async def boom(text, *, force_hermes=False, session_id=None):
        raise RuntimeError("hermes down")

    runner = voice_turn(transcript="weather?", generate_reply=boom)
    response = await runner.run()

    assert response.status == 502
    assert runner.seen[-1] == control.STATUS_CLEAR
    assert runner.gateway.voice_turn_active is False


# ---- Phase F: subtitle / route badge / LED on the response phase ------


@pytest.mark.asyncio
async def test_voice_turn_hermes_route_sets_badge_and_led(monkeypatch, voice_turn):
    runner = voice_turn(
        transcript="weather?", reply="sunny", route="hermes", route_hint="hermes"
    )
    response = await runner.run()

    assert response.status == 200
    # Subtitle: reply shown during TTS, then cleared in finally.
    assert runner.rec["subtitle"] == ["sunny", ""]
    # Badge: "H" set for Hermes, cleared in finally.
    assert runner.rec["badge"] == ["H", ""]
    # LED: listening (STT) → hermes (pre-call thinking + post-call) → idle.
    assert runner.rec["led"] == ["listening", "hermes", "hermes", "idle"]


@pytest.mark.asyncio
async def test_voice_turn_local_route_no_badge_no_led(monkeypatch, voice_turn):
    runner = voice_turn(
        transcript="hey", reply="hey", route="local", route_hint="local"
    )
    response = await runner.run()

    assert response.status == 200
    # Subtitle still shown + cleared for local turns.
    assert runner.rec["subtitle"] == ["hey", ""]
    # No badge "H" for local — only the finally clear ("").
    assert runner.rec["badge"] == [""]
    # Local keeps the listening colour (no hermes); finally restores idle.
    assert runner.rec["led"] == ["listening", "idle"]


@pytest.mark.asyncio
async def test_voice_turn_clears_cosmetics_when_tts_fails(monkeypatch, voice_turn):
    import stackchan_mcp.tts.orchestrator as tts_orch

    runner = voice_turn(transcript="weather?", reply="sunny", route="hermes")

    async def boom(arguments, *, gateway=None, **kw):
        raise RuntimeError("tts down")

    monkeypatch.setattr(tts_orch, "synthesize_and_send", boom)
    response = await runner.run()

    assert response.status == 502
    # Cosmetics were set before TTS, then the finally restores all three
    # (subtitle/badge cleared, LED back to the idle slot).
    assert runner.rec["subtitle"][-1] == ""
    assert runner.rec["badge"][-1] == ""
    assert runner.rec["led"][-1] == "idle"


# ---- conversation log recording hook ---------------------------------


@pytest.mark.asyncio
async def test_voice_turn_records_conversation(monkeypatch, voice_turn):
    control._CONVERSATION.clear()
    runner = voice_turn(transcript="good morning", reply="hey", route="local")
    response = await runner.run()

    assert response.status == 200
    turns = control.get_conversation()["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["transcript"] == "good morning"
    assert turn["reply"] == "hey"
    assert turn["route"] == "local"
    assert turn["timings_ms"] is not None and "total" in turn["timings_ms"]
    control._CONVERSATION.clear()


@pytest.mark.asyncio
async def test_voice_turn_empty_transcript_not_recorded(monkeypatch, voice_turn):
    control._CONVERSATION.clear()
    runner = voice_turn(transcript="   ")

    await runner.run()

    # An empty transcript returns before the recording hook.
    assert control.get_conversation()["turns"] == []
    control._CONVERSATION.clear()


@pytest.mark.asyncio
async def test_voice_turn_tts_failure_not_recorded(monkeypatch, voice_turn):
    import stackchan_mcp.tts.orchestrator as tts_orch

    control._CONVERSATION.clear()
    runner = voice_turn(transcript="weather?", reply="sunny", route="hermes")

    async def boom(arguments, *, gateway=None, **kw):
        raise RuntimeError("tts down")

    monkeypatch.setattr(tts_orch, "synthesize_and_send", boom)
    response = await runner.run()

    assert response.status == 502
    # A TTS failure returns before the recording hook.
    assert control.get_conversation()["turns"] == []
    control._CONVERSATION.clear()


# ---- Hermes-pin routing toggle (force_hermes) ------------------------


@pytest.mark.parametrize(
    ("force", "expected"),
    [
        # Default: the rule-based local fast-path answers.
        (False, ("local", local_llm.ROUTE_LOCAL)),
        # Pinned: the local fast-path is skipped entirely.
        (True, ("hermes", local_llm.ROUTE_HERMES)),
    ],
    ids=["default-keeps-local", "force-hermes-bypasses-local"],
)
@pytest.mark.asyncio
async def test_generate_reply_routing_by_force_flag(monkeypatch, force, expected):
    monkeypatch.setattr(local_llm, "is_enabled", lambda: True)
    monkeypatch.setattr(local_llm, "decide_route", lambda _t: local_llm.ROUTE_LOCAL)
    called = {"local": False}

    async def fake_local(text, *, system_prompt):
        called["local"] = True
        return "local"

    async def fake_hermes(text, *, session_id=None):
        return "hermes"

    monkeypatch.setattr(local_llm, "ask_local", fake_local)
    monkeypatch.setattr(hermes_bridge, "ask_hermes", fake_hermes)

    reply, route = await hermes_bridge.generate_reply("short", force_hermes=force)

    assert (reply, route) == expected
    if force:
        assert called["local"] is False  # the local fast-path was skipped


@pytest.mark.asyncio
async def test_voice_turn_force_hermes_lights_hermes_and_passes_flag(
    monkeypatch, voice_turn
):
    seen: dict[str, bool] = {}

    async def fake_gr(text, *, force_hermes=False, session_id=None):
        seen["force_hermes"] = force_hermes
        return "hello", "hermes"

    # The rule-based classifier would pick LOCAL, but the pin forces Hermes.
    runner = voice_turn(
        transcript="hey",
        reply="hello",
        route="hermes",
        route_hint="local",
        generate_reply=fake_gr,
    )
    monkeypatch.setattr(control, "routing_force_hermes", lambda: True)
    response = await runner.run()

    assert response.status == 200
    # The pin is threaded into generate_reply.
    assert seen["force_hermes"] is True
    # LED hint lights Hermes pre-call despite decide_route == local.
    assert runner.rec["led"] == ["listening", "hermes", "hermes", "idle"]


# ---- multi-turn continuation (Phase 1) -----------------------------------


def _enable_multiturn(monkeypatch, *, muted: bool = False) -> None:
    """Turn the feature on with no guard sleep, unmuted by default."""
    monkeypatch.setenv("STACKCHAN_MULTITURN", "1")
    monkeypatch.setenv("MULTITURN_TTS_GUARD_MS", "0")
    monkeypatch.setattr(control, "is_muted", lambda: muted)


@pytest.mark.asyncio
async def test_multiturn_reopens_listen_on_hermes_question(monkeypatch, voice_turn):
    runner = voice_turn(transcript="hey", reply="how have you been?", route="hermes")
    _enable_multiturn(monkeypatch)
    response = await runner.run()

    assert response.status == 200
    # A continuation listen was fired, the counter advanced, and the gap
    # flag stays True so the heartbeat is suppressed until the answer.
    assert runner.gateway.esp32.listen_calls == [("start", "manual")]
    assert runner.gateway.multiturn.turn_count == 1
    assert runner.gateway.multiturn_active is True
    body = json.loads(response.body)
    assert body["multiturn"] is True


@pytest.mark.asyncio
async def test_multiturn_off_by_default(monkeypatch, voice_turn):
    # No STACKCHAN_MULTITURN env: feature disabled even on a question.
    monkeypatch.delenv("STACKCHAN_MULTITURN", raising=False)
    runner = voice_turn(transcript="hey", reply="how are you?", route="hermes")
    response = await runner.run()

    assert response.status == 200
    assert runner.gateway.esp32.listen_calls == []
    assert runner.gateway.multiturn_active is False
    assert json.loads(response.body)["multiturn"] is False


@pytest.mark.parametrize(
    ("route", "reply", "muted", "connected"),
    [
        ("local", "how are you?", False, True),  # brain chose the local route
        ("hermes", "I see.", False, True),  # Hermes answered, no question
        ("hermes", "how are you?", True, True),  # device muted
        ("hermes", "how are you?", False, False),  # device disconnected
    ],
    ids=["local-route", "non-question", "muted", "disconnected"],
)
@pytest.mark.asyncio
async def test_multiturn_skips_relisten(
    monkeypatch, voice_turn, route, reply, muted, connected
):
    """No hands-free re-listen fires for any non-continuable turn."""
    runner = voice_turn(transcript="hey", reply=reply, route=route)
    _enable_multiturn(monkeypatch, muted=muted)
    runner.gateway.esp32.device_connected = connected

    await runner.run()

    assert runner.gateway.esp32.listen_calls == []
    assert runner.gateway.multiturn_active is False


@pytest.mark.asyncio
async def test_multiturn_stops_at_ceiling(monkeypatch, voice_turn):
    runner = voice_turn(transcript="hey", reply="keep going?", route="hermes")
    _enable_multiturn(monkeypatch)
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "2")
    # Mid-conversation at the ceiling: a *fresh* gap (recent activity) so
    # the entry stale-reset does not fire and the ceiling check applies.
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 500.0)
    runner.gateway.multiturn.turn_count = 2  # already at the ceiling
    runner.gateway.multiturn.last_activity = 500.0

    await runner.run()

    assert runner.gateway.esp32.listen_calls == []
    # Reaching the ceiling ends the conversation: the counter resets.
    assert runner.gateway.multiturn.turn_count == 0


@pytest.mark.asyncio
async def test_multiturn_ceiling_on_question_shows_tap_hint(monkeypatch, voice_turn):
    # Phase 3 UX: when we stop only because the turn ceiling was hit while
    # Hermes still had an open question, leave a "tap to continue" subtitle
    # instead of blanking the display.
    runner = voice_turn(transcript="hey", reply="keep going?", route="hermes")
    _enable_multiturn(monkeypatch)
    monkeypatch.setenv("MAX_MULTITURN_TURNS", "2")
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 500.0)
    runner.gateway.multiturn.turn_count = 2  # already at the ceiling
    runner.gateway.multiturn.last_activity = 500.0

    await runner.run()

    assert runner.gateway.esp32.listen_calls == []  # no hands-free re-listen
    assert runner.gateway.multiturn_active is False
    # The last subtitle written is the hint (after the reply subtitle), and
    # the one-shot flag is consumed.
    assert runner.rec["subtitle"][-1] == multiturn.TAP_TO_CONTINUE_HINT
    assert runner.gateway.multiturn_prompt_pending is False


@pytest.mark.asyncio
async def test_multiturn_no_hint_on_normal_end(monkeypatch, voice_turn):
    # A non-question end clears the subtitle as before — the hint is only
    # for the ceiling-on-question case, not every conversation close.
    runner = voice_turn(transcript="hey", reply="right.", route="hermes")
    _enable_multiturn(monkeypatch)

    await runner.run()

    assert runner.gateway.multiturn_prompt_pending is False
    assert runner.rec["subtitle"][-1] == ""


@pytest.mark.asyncio
async def test_multiturn_empty_transcript_resets_counter(monkeypatch, voice_turn):
    runner = voice_turn(transcript="   ", reply="ignored", route="hermes")
    _enable_multiturn(monkeypatch)
    runner.gateway.multiturn.turn_count = 2  # mid-conversation

    response = await runner.run()

    # Silence ends the conversation: no re-listen, counter cleared.
    assert response.status == 200
    assert json.loads(response.body)["ok"] is False
    assert runner.gateway.esp32.listen_calls == []
    assert runner.gateway.multiturn.turn_count == 0


@pytest.mark.asyncio
async def test_voice_turn_threads_rotating_conversation_id(monkeypatch, voice_turn):
    """Phase 2: the voice turn threads a per-conversation Hermes id into
    the brain call — reused within the context window, rotated past it."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)  # default base
    monkeypatch.delenv("HERMES_SESSION_WINDOW_S", raising=False)  # default 180
    seen: list[str | None] = []

    async def capture_gr(text, *, force_hermes=False, session_id=None):
        seen.append(session_id)
        return "yes", "hermes"

    runner = voice_turn(
        transcript="hey", reply="yes", route="hermes", generate_reply=capture_gr
    )
    # Two turns 10 s apart share one conversation id (< 180 s window)...
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1000.0)
    await runner.run()
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1010.0)
    await runner.run()
    # ...then a long gap starts a fresh conversation (new id).
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 5000.0)
    await runner.run()

    assert all(s and s.startswith("stackchan-voice-") for s in seen)
    assert seen[0] == seen[1]  # same conversation, context retained
    assert seen[2] != seen[0]  # rotated after the window


@pytest.mark.asyncio
async def test_voice_turn_window_zero_uses_fixed_session_id(monkeypatch, voice_turn):
    """HERMES_SESSION_WINDOW_S=0 disables rotation — every turn carries
    the fixed HERMES_SESSION_ID, exactly as before Phase 2."""
    monkeypatch.setenv("HERMES_SESSION_ID", "stackchan-voice")
    monkeypatch.setenv("HERMES_SESSION_WINDOW_S", "0")
    seen: list[str | None] = []

    async def capture_gr(text, *, force_hermes=False, session_id=None):
        seen.append(session_id)
        return "yes", "hermes"

    runner = voice_turn(
        transcript="hey", reply="yes", route="hermes", generate_reply=capture_gr
    )

    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1000.0)
    await runner.run()
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 9000.0)
    await runner.run()

    # No rotation: the base id is used verbatim on every turn.
    assert seen == ["stackchan-voice", "stackchan-voice"]


@pytest.mark.asyncio
async def test_multiturn_stale_gap_resets_at_turn_entry(monkeypatch, voice_turn):
    runner = voice_turn(transcript="hey", reply="right.", route="hermes")
    _enable_multiturn(monkeypatch)
    monkeypatch.setenv("MULTITURN_SESSION_TIMEOUT_S", "60")
    # An old, abandoned gap: counter set, activity far in the past.
    runner.gateway.multiturn.turn_count = 3
    runner.gateway.multiturn.last_activity = 1.0
    monkeypatch.setattr(hermes_bridge.time, "monotonic", lambda: 1000.0)

    await runner.run()

    # The stale gap was reset at entry; this fresh turn (no question)
    # leaves the counter at 0.
    assert runner.gateway.multiturn.turn_count == 0
    assert runner.gateway.multiturn_active is False


@pytest.mark.asyncio
async def test_multiturn_continuation_skips_display_clear(monkeypatch, voice_turn):
    # When a turn re-opens listening, the finally must NOT clear the
    # status text (on_listen_started owns the listening display now).
    runner = voice_turn(transcript="hey", reply="how are you?", route="hermes")
    _enable_multiturn(monkeypatch)

    await runner.run()

    # I'm listening... → Thinking..., but NO trailing clear (would blank the re-listen).
    assert control.STATUS_CLEAR not in runner.seen
    assert runner.seen == [control.STATUS_LISTENING, control.STATUS_THINKING]
