"""Tests for local_llm: route decision, Ollama call, Hermes fallback."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from aiohttp import web

from stackchan_mcp import hermes_bridge, local_llm
from stackchan_mcp.local_llm import (
    DEFAULT_LOCAL_LLM_TIMEOUT_S,
    LOCAL_MAX_CHARS,
    ROUTE_HERMES,
    ROUTE_LOCAL,
    _env_float,
    _is_date_query,
    ask_local,
    decide_route,
    is_enabled,
)


# --- decide_route (pure routing policy) --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "good morning",
        "hello, how are you?",
        "what day is today?",
        "thank you",
        "go to sleep",
    ],
)
def test_decide_route_short_simple_goes_local(text):
    """Greetings and quick chat stay local."""
    assert decide_route(text) == ROUTE_LOCAL


@pytest.mark.parametrize(
    "text",
    [
        "what's the weather tomorrow",
        "any latest news?",
        "look up ESP32 Opus encoding",
        "how does my schedule look this week?",
        "why is the sky blue?",
        "explain why that happens",
        "what's your opinion on this?",
        "remind me later",
        "do you remember what we talked about?",
        # appliance control needs the gateway's switchbot_* tools, which
        # only Hermes can call — short command phrases must not go local
        "turn on the lights",
        "turn off the living room lights",
        "turn off the aircon",
        "turn on the TV",
        "show the SwitchBot devices",
        # notes / web_search tools (Phase D) live on Hermes too; "memo"
        # and "list" also catch common STT mangling of those requests
        # (e.g. "add milk to the shopping list and make a memo" →
        #  "please note the milk")
        "add milk to the shopping list and make a memo",
        "please note that down for me",
        "read my memo",
        # request-shaped utterances imply actions, and actions need
        # tools — including STT-mangled forms that lost their original
        # marker word (observed live in the Phase D2 E2E)
        "please jot milk down",
        "please save that",
        "turn on the device, please",
    ],
)
def test_decide_route_markers_go_hermes(text):
    """Tool/memory/deliberation markers force Hermes regardless of length."""
    assert decide_route(text) == ROUTE_HERMES


def test_decide_route_long_text_goes_hermes():
    """Past LOCAL_MAX_CHARS the turn carries real content — Hermes."""
    text = "so, " + "today a lot of things happened, " * 5 + "listen?"
    assert len(text) > LOCAL_MAX_CHARS
    assert decide_route(text) == ROUTE_HERMES


def test_decide_route_boundary_length():
    """Exactly LOCAL_MAX_CHARS chars is still local; one more is not."""
    at_limit = "a" * LOCAL_MAX_CHARS
    assert decide_route(at_limit) == ROUTE_LOCAL
    assert decide_route(at_limit + "a") == ROUTE_HERMES


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_decide_route_empty_goes_hermes(text):
    """Empty / whitespace-only input falls through to Hermes (defensive)."""
    assert decide_route(text) == ROUTE_HERMES


def test_decide_route_nfkc_normalisation():
    """Full-width / half-width variants of a marker still match."""
    # NFKC folds full-width Latin so "\uff57\uff45\uff41\uff54\uff48\uff45\uff52"
    # ("weather") must hit the weather marker after normalisation.
    assert decide_route("\uff57\uff45\uff41\uff54\uff48\uff45\uff52?") == ROUTE_HERMES


# --- _is_date_query (date-context injection gate) ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what day is today?",
        "what's today's date?",
        "what date is it?",
        "tell me the date",
        "do you know the date?",
        "what day of the week is it?",
        "what weekday is it?",
        "today is what day",
        "tell me today's schedule",  # today still counts as a date reference
        "What's the date today?",
        "what day is it today",
    ],
)
def test_is_date_query_true(text):
    assert _is_date_query(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "good morning",
        "uh",
        "turn on the lights",
        "thank you",
        "hello, how are you?",
        "go to sleep",
        "",
        "   ",
    ],
)
def test_is_date_query_false(text):
    assert _is_date_query(text) is False


def test_is_date_query_nfkc_folds_fullwidth():
    """Full-width 'today' folds to the ASCII marker after NFKC."""
    assert _is_date_query("\uff54\uff4f\uff44\uff41\uff59?") is True


# --- is_enabled (opt-in gate) -------------------------------------------------


def test_is_enabled_requires_model_env(monkeypatch):
    monkeypatch.delenv("STACKCHAN_LOCAL_LLM_MODEL", raising=False)
    assert is_enabled() is False
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "")
    assert is_enabled() is False
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "   ")
    assert is_enabled() is False
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "some-model:q4")
    assert is_enabled() is True


# --- _env_float (timeout parse guard) ----------------------------------------


def test_env_float_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("STACKCHAN_LOCAL_LLM_TIMEOUT_S", raising=False)
    assert _env_float("STACKCHAN_LOCAL_LLM_TIMEOUT_S", 10.0) == 10.0


def test_env_float_parses_valid_value(monkeypatch):
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_TIMEOUT_S", "3.5")
    assert _env_float("STACKCHAN_LOCAL_LLM_TIMEOUT_S", 10.0) == 3.5


def test_env_float_invalid_falls_back_and_warns(monkeypatch, caplog):
    """A non-numeric timeout must not raise; warn and use the default."""
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_TIMEOUT_S", "abc")
    with caplog.at_level(logging.WARNING):
        value = _env_float(
            "STACKCHAN_LOCAL_LLM_TIMEOUT_S", DEFAULT_LOCAL_LLM_TIMEOUT_S
        )
    assert value == DEFAULT_LOCAL_LLM_TIMEOUT_S
    assert "STACKCHAN_LOCAL_LLM_TIMEOUT_S" in caplog.text


async def test_ask_local_survives_invalid_timeout_env(
    monkeypatch, aiohttp_unused_port
):
    """A garbage timeout env must not abort the call — it falls back to
    the default and the turn still completes (rather than raising
    ValueError and silently forcing every turn onto Hermes)."""

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response(
            {"message": {"role": "assistant", "content": "hey"}}
        )

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_TIMEOUT_S", "abc")
    try:
        reply = await ask_local("good morning", system_prompt="be brief.")
    finally:
        await runner.cleanup()
    assert reply == "hey"


# --- ask_local (Ollama /api/chat) ---------------------------------------------


async def _run_ollama_stub(
    handler, aiohttp_unused_port
) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/api/chat", handler)
    port = aiohttp_unused_port()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, f"http://127.0.0.1:{port}"


async def test_ask_local_success(monkeypatch, aiohttp_unused_port):
    """Happy path: payload carries model / stream=false / keep_alive and
    the system prompt; reply text comes back. A non-date turn must NOT
    carry the date context (the model can't keep it secret otherwise)."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        return web.json_response(
            {"message": {"role": "assistant", "content": " hello! "}}
        )

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    try:
        reply = await ask_local("hello", system_prompt="speak briefly.")
    finally:
        await runner.cleanup()

    assert reply == "hello!"
    payload = received["payload"]
    assert payload["model"] == "test-model:q4"
    assert payload["stream"] is False
    assert payload["keep_alive"] == local_llm.DEFAULT_LOCAL_LLM_KEEP_ALIVE
    system = payload["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith("speak briefly.")
    # non-date turn: date context must be absent so the model can't blurt it
    assert "weekday)" not in system["content"]
    # no-tools guard: the local model must not pretend to run tools
    assert local_llm.LOCAL_NO_TOOLS_LINE in system["content"]
    assert payload["messages"][1] == {"role": "user", "content": "hello"}


async def test_ask_local_injects_date_only_on_date_query(
    monkeypatch, aiohttp_unused_port
):
    """A date/weekday question gets today's date injected into the prompt."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        return web.json_response(
            {"message": {"role": "assistant", "content": "today is Thursday!"}}
        )

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    try:
        await ask_local("what day is today?", system_prompt="speak briefly.")
    finally:
        await runner.cleanup()

    system = received["payload"]["messages"][0]["content"]
    assert "Today's date is" in system   # assertive date context injected
    assert local_llm.LOCAL_NO_TOOLS_LINE in system


async def test_ask_local_no_date_on_vague_turn(monkeypatch, aiohttp_unused_port):
    """Regression: a vague non-date turn ("uh") must not carry the date,
    so the local model cannot volunteer it unprompted."""
    received: dict[str, Any] = {}

    async def handle(request: web.Request) -> web.Response:
        received["payload"] = await request.json()
        return web.json_response(
            {"message": {"role": "assistant", "content": "what's up?"}}
        )

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    try:
        await ask_local("uh", system_prompt="speak briefly.")
    finally:
        await runner.cleanup()

    system = received["payload"]["messages"][0]["content"]
    assert "Today's date is" not in system
    assert "weekday" not in system


async def test_ask_local_strips_think_tags(monkeypatch, aiohttp_unused_port):
    """Reasoning-model <think> blocks never reach the TTS pipeline."""

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response(
            {
                "message": {
                    "role": "assistant",
                    "content": "<think>some long thinking...\nwith newline</think>yes, Thursday.",
                }
            }
        )

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    try:
        reply = await ask_local("what day is today?", system_prompt="be brief.")
    finally:
        await runner.cleanup()

    assert reply == "yes, Thursday."


async def test_ask_local_error_status_raises(monkeypatch, aiohttp_unused_port):
    """Non-200 from Ollama raises RuntimeError (caller falls back)."""

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"error": "model not found"}, status=404)

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "missing-model")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    try:
        with pytest.raises(RuntimeError, match="status=404"):
            await ask_local("good morning", system_prompt="be brief.")
    finally:
        await runner.cleanup()


async def test_ask_local_empty_reply_raises(monkeypatch, aiohttp_unused_port):
    """Empty / missing content raises RuntimeError (caller falls back)."""

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"message": {"role": "assistant", "content": ""}})

    runner, base_url = await _run_ollama_stub(handle, aiohttp_unused_port)
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_URL", base_url)
    try:
        with pytest.raises(RuntimeError, match="empty reply"):
            await ask_local("good morning", system_prompt="be brief.")
    finally:
        await runner.cleanup()


async def test_ask_local_without_model_raises(monkeypatch):
    monkeypatch.delenv("STACKCHAN_LOCAL_LLM_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="STACKCHAN_LOCAL_LLM_MODEL"):
        await ask_local("good morning", system_prompt="be brief.")


# --- generate_reply (routing + fallback in the voice bridge) ------------------


async def test_generate_reply_disabled_uses_hermes(monkeypatch):
    """Without STACKCHAN_LOCAL_LLM_MODEL the local path is never touched —
    identical to the pre-routing behaviour."""
    monkeypatch.delenv("STACKCHAN_LOCAL_LLM_MODEL", raising=False)
    calls: list[str] = []

    async def fake_hermes(
        text: str, *, session_id: str | None = None, on_step: Any | None = None
    ) -> str:
        calls.append(text)
        return "hermes reply"

    async def fail_local(text: str, *, system_prompt: str) -> str:
        raise AssertionError("local path must not be called when disabled")

    monkeypatch.setattr(hermes_bridge, "ask_hermes_stream", fake_hermes)
    monkeypatch.setattr(local_llm, "ask_local", fail_local)

    reply, route = await hermes_bridge.generate_reply("good morning")
    assert (reply, route) == ("hermes reply", "hermes")
    assert calls == ["good morning"]


async def test_generate_reply_routes_short_turn_local(monkeypatch):
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")

    async def fail_hermes(
        text: str, *, session_id: str | None = None, on_step: Any | None = None
    ) -> str:
        raise AssertionError("Hermes must not be called on the local route")

    async def fake_local(text: str, *, system_prompt: str) -> str:
        assert text == "good morning"
        assert system_prompt   # voice constraints are passed through
        return "local reply"

    monkeypatch.setattr(hermes_bridge, "ask_hermes_stream", fail_hermes)
    monkeypatch.setattr(local_llm, "ask_local", fake_local)

    reply, route = await hermes_bridge.generate_reply("good morning")
    assert (reply, route) == ("local reply", "local")


async def test_generate_reply_long_turn_goes_hermes(monkeypatch):
    """Routing enabled, but a deliberation-grade turn still goes to Hermes."""
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")

    async def fake_hermes(
        text: str, *, session_id: str | None = None, on_step: Any | None = None
    ) -> str:
        return "hermes reply"

    async def fail_local(text: str, *, system_prompt: str) -> str:
        raise AssertionError("local path must not be called for Hermes turns")

    monkeypatch.setattr(hermes_bridge, "ask_hermes_stream", fake_hermes)
    monkeypatch.setattr(local_llm, "ask_local", fail_local)

    reply, route = await hermes_bridge.generate_reply("check the weather tomorrow")
    assert (reply, route) == ("hermes reply", "hermes")


async def test_generate_reply_local_failure_falls_back(monkeypatch):
    """Ollama down / timeout / bad reply → the turn survives via Hermes."""
    monkeypatch.setenv("STACKCHAN_LOCAL_LLM_MODEL", "test-model:q4")

    async def fake_hermes(
        text: str, *, session_id: str | None = None, on_step: Any | None = None
    ) -> str:
        return "hermes reply"

    async def broken_local(text: str, *, system_prompt: str) -> str:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(hermes_bridge, "ask_hermes_stream", fake_hermes)
    monkeypatch.setattr(local_llm, "ask_local", broken_local)

    reply, route = await hermes_bridge.generate_reply("good morning")
    assert (reply, route) == ("hermes reply", "hermes")


async def test_generate_reply_hermes_failure_still_raises(monkeypatch):
    """A Hermes failure propagates as before — fallback only covers local."""
    monkeypatch.delenv("STACKCHAN_LOCAL_LLM_MODEL", raising=False)

    async def broken_hermes(
        text: str, *, session_id: str | None = None, on_step: Any | None = None
    ) -> str:
        raise RuntimeError("Hermes API returned status=500")

    monkeypatch.setattr(hermes_bridge, "ask_hermes_stream", broken_hermes)
    with pytest.raises(RuntimeError, match="status=500"):
        await hermes_bridge.generate_reply("good morning")


# --- helpers ------------------------------------------------------------------


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
