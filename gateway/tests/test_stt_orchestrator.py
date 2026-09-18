"""Tests for the STT orchestrator pipeline (Issue #91).

Symmetric to :mod:`tests.test_orchestrator` (the TTS counterpart).
Focuses on the pipeline shape — argument validation, listen-state
notifications, protocol-v1 gate, listen_lock serialisation, empty
captures, and clean error translation — without depending on the
heavy ML engines or libopus.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import stackchan_mcp.stt.orchestrator as orchestrator
from stackchan_mcp.audio_stream import is_recording, start_recording, stop_recording
from stackchan_mcp.stt import EngineRegistry, STTEngine, listen_and_transcribe
from stackchan_mcp.stt.audio_utils import DEVICE_FRAME_DURATION_MS, DEVICE_SAMPLE_RATE


class _CapturingEngine(STTEngine):
    """Engine that returns fixed text and records what it received."""

    def __init__(self, text: str = "hello", name: str = "faster-whisper") -> None:
        self.name = name
        self._text = text
        self.calls: list[tuple[bytes, dict[str, Any]]] = []

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        self.calls.append((pcm, dict(opts)))
        return {"text": self._text, "language": opts.get("language") or "ja"}


class _RaisingEngine(STTEngine):
    """Engine that always raises a configured exception."""

    def __init__(self, exc: Exception, name: str = "faster-whisper") -> None:
        self.name = name
        self._exc = exc

    async def transcribe(self, pcm: bytes, **opts: Any) -> dict[str, Any]:
        raise self._exc


class _FakeESP32:
    def __init__(
        self,
        *,
        connected: bool = True,
        protocol_version: int = 1,
        frames_to_inject: list[bytes] | None = None,
        injection_delay_s: float = 0.0,
    ) -> None:
        self.device_connected = connected
        self.connection = SimpleNamespace(
            protocol_version=protocol_version,
            session_id="session-test",
        )
        self.listen_states: list[tuple[str, str | None]] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, Any]] = []
        self.listen_lock = asyncio.Lock()
        self.head_yaw = 12.0
        self.head_pitch = 24.0
        self._frames_to_inject = list(frames_to_inject or [])
        self._injection_delay_s = injection_delay_s

    async def send_listen_state(self, state: str, mode: str = "manual") -> None:
        self.listen_states.append((state, mode if state == "start" else None))
        self.events.append(("listen_state", state))
        if state == "start" and self._frames_to_inject:
            # Schedule frame injection while the orchestrator is in the
            # capture window; create_task makes the injection run
            # concurrently with the orchestrator's asyncio.sleep.
            asyncio.create_task(self._inject_frames())

    async def _inject_frames(self) -> None:
        # Delay slightly so the orchestrator has marked recording
        # active; the 50 ms transition-delay sleep is plenty, and we
        # yield once to stay deterministic regardless of scheduling.
        await asyncio.sleep(self._injection_delay_s)
        from stackchan_mcp.audio_stream import handle_audio_frame

        for frame in self._frames_to_inject:
            await handle_audio_frame(frame, "session-test")

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], None]:
        self.tool_calls.append((name, dict(arguments)))
        self.events.append(("tool", name))
        if name == "self.robot.get_head_angles":
            return {"yaw": self.head_yaw, "pitch": self.head_pitch}, None
        if name == "self.robot.set_head_angles":
            self.head_yaw = float(arguments["yaw"])
            self.head_pitch = float(arguments["pitch"])
            return {"ok": True}, None
        if name == "self.display.set_avatar":
            return {"ok": True}, None
        raise AssertionError(f"unexpected tool call: {name}")


class _FakeGateway:
    def __init__(self, esp32: _FakeESP32) -> None:
        self.esp32 = esp32


def _build_env(
    *,
    text: str = "hello",
    exc: Exception | None = None,
    esp32_cls: type[_FakeESP32] = _FakeESP32,
    **esp32_kwargs: Any,
) -> SimpleNamespace:
    """Build a registered fake engine + ESP32 + gateway.

    Returns a namespace with ``engine``, ``esp32``, ``gateway`` and
    ``reg`` ready to hand to :func:`listen_and_transcribe`."""

    engine = _CapturingEngine(text=text) if exc is None else _RaisingEngine(exc)
    esp32 = esp32_cls(**esp32_kwargs)
    gateway = _FakeGateway(esp32)
    reg = EngineRegistry()
    reg.register(engine)
    return SimpleNamespace(engine=engine, esp32=esp32, gateway=gateway, reg=reg)


@pytest.fixture
def fake_decode(monkeypatch):
    """Replace decode_opus_frames so tests don't need libopus.

    Concatenates frame payloads as-is; for the orchestrator's purposes
    the exact PCM contents don't matter beyond "non-empty when frames
    arrived, empty when none did".
    """

    def fake(frames, **kwargs):
        return b"".join(frames)

    monkeypatch.setattr(orchestrator, "decode_opus_frames", fake)
    return fake


@pytest.fixture
def fast_sleep(monkeypatch):
    """Compress the capture-window sleep so tests run instantly."""

    real_sleep = asyncio.sleep

    async def fast(delay):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast)
    return fast


@pytest.fixture(autouse=True)
def _cleanup_recording_slot():
    """Always release the module-level recording slot between tests.

    The orchestrator opens/closes the slot itself, but a failed
    test that bypasses ``finally`` would leak state into the next
    test; this fixture defends against that.
    """
    yield
    if is_recording():
        stop_recording()


@pytest.mark.asyncio
async def test_pipeline_drives_listen_state_and_returns_text(fake_decode, fast_sleep):
    """Happy path: start/stop notifications fire, frames decode, engine runs."""
    frames = [b"opus_frame_0", b"opus_frame_1", b"opus_frame_2"]
    env = _build_env(text="yaho", frames_to_inject=frames)

    result = await listen_and_transcribe(
        {"duration_ms": 500, "engine": "faster-whisper", "language": "ja"},
        gateway=env.gateway,
        registry=env.reg,
    )

    assert [s[0] for s in env.esp32.listen_states] == ["start", "stop"]
    # start was sent with mode="manual"; stop carries no mode.
    assert env.esp32.listen_states[0] == ("start", "manual")
    assert env.esp32.listen_states[1] == ("stop", None)

    assert result["engine"] == "faster-whisper"
    assert result["text"] == "yaho"
    assert result["language"] == "ja"
    assert result["frame_count"] == 3
    assert result["duration_ms"] == 3 * DEVICE_FRAME_DURATION_MS
    assert result["sample_rate"] == DEVICE_SAMPLE_RATE

    # Engine saw the concatenated PCM (our fake decode glued the frame
    # payloads together).
    assert len(env.engine.calls) == 1
    pcm_arg, opts = env.engine.calls[0]
    assert pcm_arg == b"".join(frames)
    assert opts["language"] == "ja"

    # Recording slot was closed cleanly.
    assert not is_recording()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("motion", "expected_tool_calls"),
    [
        ("none", []),
        (
            "face-only",
            [
                ("self.display.set_avatar", {"face": "thinking"}),
                ("self.display.set_avatar", {"face": "idle"}),
            ],
        ),
        (
            "look-up",
            [
                ("self.robot.get_head_angles", {}),
                ("self.robot.set_head_angles", {"yaw": 12.0, "pitch": 50.0}),
                ("self.display.set_avatar", {"face": "thinking"}),
            ],
        ),
    ],
)
async def test_listen_motion_success_paths(
    fake_decode, fast_sleep, motion, expected_tool_calls
):
    """Each motion mode preserves its success cleanup/hold contract."""
    env = _build_env(text="ok", frames_to_inject=[b"opus_a"])

    result = await listen_and_transcribe(
        {"duration_ms": 500, "motion": motion},
        gateway=env.gateway,
        registry=env.reg,
    )

    assert result["text"] == "ok"
    assert [s[0] for s in env.esp32.listen_states] == ["start", "stop"]
    assert env.esp32.tool_calls == expected_tool_calls
    if motion == "look-up":
        assert env.esp32.head_pitch == 50.0
    else:
        assert env.esp32.head_pitch == 24.0
    assert not is_recording()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("motion", "expected_tool_calls"),
    [
        ("none", []),
        (
            "face-only",
            [
                ("self.display.set_avatar", {"face": "thinking"}),
                ("self.display.set_avatar", {"face": "idle"}),
            ],
        ),
        (
            "look-up",
            [
                ("self.robot.get_head_angles", {}),
                ("self.robot.set_head_angles", {"yaw": 12.0, "pitch": 50.0}),
                ("self.display.set_avatar", {"face": "thinking"}),
                ("self.robot.set_head_angles", {"yaw": 12.0, "pitch": 24.0}),
                ("self.display.set_avatar", {"face": "idle"}),
            ],
        ),
    ],
)
async def test_listen_motion_failure_paths(
    fake_decode, fast_sleep, motion, expected_tool_calls
):
    """Failures clean up avatar state and roll back look-up pitch."""
    env = _build_env(exc=TimeoutError("model timed out"), frames_to_inject=[b"opus_a"])

    with pytest.raises(RuntimeError, match="failed"):
        await listen_and_transcribe(
            {"duration_ms": 500, "motion": motion},
            gateway=env.gateway,
            registry=env.reg,
        )

    assert [s[0] for s in env.esp32.listen_states] == ["start", "stop"]
    assert env.esp32.tool_calls == expected_tool_calls
    assert env.esp32.head_pitch == 24.0
    assert not is_recording()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listen_args", "error_match"),
    [
        ({"duration_ms": 500, "motion": "none", "look_up_pitch": 4.0}, "look_up_pitch"),
        (
            {"duration_ms": 500, "motion": "face-only", "look_up_pitch": 4.0},
            "look_up_pitch",
        ),
        (
            {"duration_ms": 500, "motion": "look-up", "look_up_pitch": 4.0},
            "look_up_pitch",
        ),
        ({"duration_ms": 500, "motion": "nod"}, "motion"),
    ],
)
async def test_listen_rejects_invalid_options_before_any_device_call(
    listen_args, error_match
):
    """Invalid motion/look_up_pitch options fail before any device-side call."""
    env = _build_env(frames_to_inject=[b"opus_a"])

    with pytest.raises(ValueError, match=error_match):
        await listen_and_transcribe(listen_args, gateway=env.gateway, registry=env.reg)

    assert env.esp32.listen_states == []
    assert env.esp32.tool_calls == []
    assert env.engine.calls == []
    assert not is_recording()


@pytest.mark.asyncio
@pytest.mark.parametrize("motion", ["face-only", "look-up"])
async def test_listen_motion_cleanup_completes_under_cancellation(fake_decode, motion):
    """Cancellation during capture must not bypass motion cleanup.

    The avatar/head rollback must complete before ``listen_lock`` is
    released; a competing waiter racing for the lock must observe the
    cleanup events already set. A naïve ``except Exception`` around
    ``asyncio.shield(...)`` would orphan cleanup after the lock is
    released and fail these assertions.
    """
    env = _build_env(text="ok", frames_to_inject=[b"opus_a"])

    cleanup_idle_observed = asyncio.Event()
    cleanup_pitch_restored = asyncio.Event()
    original_call_tool = env.esp32.call_tool

    async def slow_cleanup(name, arguments):
        result_pair = await original_call_tool(name, arguments)
        # Add a small delay on the cleanup-path calls so we can
        # observe whether the orchestrator waits for them.
        if name == "self.display.set_avatar" and arguments.get("face") == "idle":
            await asyncio.sleep(0.02)
            cleanup_idle_observed.set()
        if name == "self.robot.set_head_angles" and arguments.get("pitch") == 24.0:
            await asyncio.sleep(0.02)
            cleanup_pitch_restored.set()
        return result_pair

    env.esp32.call_tool = slow_cleanup

    listen_task = asyncio.create_task(
        listen_and_transcribe(
            {"duration_ms": 200, "motion": motion},
            gateway=env.gateway,
            registry=env.reg,
        )
    )

    # Let the orchestrator enter the capture window and acquire
    # listen_lock before launching the waiter / cancelling.
    await asyncio.sleep(0.02)

    waiter_snapshot: dict[str, bool] = {}

    async def waiter() -> None:
        async with env.esp32.listen_lock:
            # Snapshot at the exact moment the lock is acquired — the
            # moment a buggy implementation would let the waiter
            # through while cleanup is still in flight.
            waiter_snapshot["idle"] = cleanup_idle_observed.is_set()
            waiter_snapshot["pitch"] = cleanup_pitch_restored.is_set()

    waiter_task = asyncio.create_task(waiter())
    # Yield once so the waiter registers its lock request before the
    # orchestrator releases the lock.
    await asyncio.sleep(0)

    async def re_cancel() -> None:
        # Re-cancel while the orchestrator is already inside the
        # motion-cleanup await. With a naïve
        # ``try / await asyncio.shield(coro()) / except Exception``
        # wrapper this second cancellation orphans the in-flight
        # cleanup before listen_lock is released — the precise
        # regression this test protects against.
        await asyncio.sleep(0.005)
        if not listen_task.done():
            listen_task.cancel()

    re_cancel_task = asyncio.create_task(re_cancel())

    listen_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await listen_task
    await waiter_task
    await re_cancel_task

    assert waiter_snapshot["idle"], (
        "set_avatar('idle') must complete BEFORE listen_lock is released "
        "to the competing waiter — orphan cleanup race detected"
    )
    if motion == "look-up":
        assert waiter_snapshot["pitch"], (
            "saved pitch must be restored BEFORE listen_lock is released "
            "to the competing waiter"
        )
    assert not is_recording()


@pytest.mark.asyncio
async def test_listen_motion_look_up_re_cancel_during_cleanup_chains_rollback_failure(
    fake_decode,
):
    """A re-entrant cancel during the motion-cleanup await must chain the
    rollback failure onto the ``CancelledError`` the caller sees rather
    than surfacing a bare cancellation.
    """
    env = _build_env(text="ok", frames_to_inject=[b"opus_a"])

    original_call_tool = env.esp32.call_tool

    async def slow_failing_rollback(name, arguments):
        # Slow the rollback so the re-cancel lands while the
        # cleanup_task is still in flight.
        if name == "self.robot.set_head_angles" and arguments.get("pitch") == 24.0:
            await asyncio.sleep(0.03)
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "simulated rollback pitch failure"}
        return await original_call_tool(name, arguments)

    env.esp32.call_tool = slow_failing_rollback

    listen_task = asyncio.create_task(
        listen_and_transcribe(
            {"duration_ms": 200, "motion": "look-up"},
            gateway=env.gateway,
            registry=env.reg,
        )
    )

    # Let the orchestrator enter the capture window.
    await asyncio.sleep(0.02)

    async def re_cancel() -> None:
        # Re-cancel after the first cancel has propagated past the
        # capture sleep and the orchestrator is inside
        # ``_shield_listen_motion_cleanup``'s shield-await loop.
        await asyncio.sleep(0.005)
        if not listen_task.done():
            listen_task.cancel()

    re_cancel_task = asyncio.create_task(re_cancel())
    listen_task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await listen_task
    await re_cancel_task

    # Cleanup failure must be chained onto the cancellation — otherwise
    # the caller has no programmatic signal about an off-baseline pose.
    primary = exc_info.value
    chained = primary.__cause__
    assert chained is not None, (
        "rollback failure must be chained onto the cancellation even "
        "when _shield_listen_motion_cleanup raises a fresh CancelledError"
    )
    assert "set_head_angles" in str(chained), (
        f"chained error should reference the rollback head-angles failure; "
        f"got {chained!r}"
    )


@pytest.mark.asyncio
async def test_listen_motion_look_up_double_cleanup_failure_preserves_both_errors(
    fake_decode,
    fast_sleep,
):
    """Both pitch-rollback and avatar-restore failures must remain
    inspectable: avatar failure chained via ``__cause__``, pitch failure
    on its ``__context__`` (finally semantics), so the caller sees
    primary ⟶ __cause__ (avatar) ⟶ __context__ (pitch).
    """
    env = _build_env(exc=TimeoutError("engine fail"), frames_to_inject=[b"opus_a"])

    original_call_tool = env.esp32.call_tool

    async def double_cleanup_failure(name, arguments):
        # Rollback set_head_angles(saved pitch=24.0) fails.
        if name == "self.robot.set_head_angles" and arguments.get("pitch") == 24.0:
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "rollback pitch failure"}
        # Cleanup-path set_avatar('idle') also fails.
        if name == "self.display.set_avatar" and arguments.get("face") == "idle":
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "rollback avatar failure"}
        return await original_call_tool(name, arguments)

    env.esp32.call_tool = double_cleanup_failure

    with pytest.raises(RuntimeError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 500, "motion": "look-up"},
            gateway=env.gateway,
            registry=env.reg,
        )

    primary = exc_info.value
    assert "engine" in str(primary).lower(), (
        f"primary should reference the engine failure; got {primary!r}"
    )

    chained = primary.__cause__
    assert chained is not None, "cleanup chain must reach the caller"
    assert "set_avatar" in str(chained), (
        f"primary cleanup error should be the avatar restore failure; got {chained!r}"
    )

    # The pitch failure is preserved via Python's automatic
    # exception-context tracking, so the caller can navigate to it.
    pitch_failure = chained.__context__
    assert pitch_failure is not None, (
        "pitch rollback failure must remain inspectable via __cause__.__context__"
    )
    assert "set_head_angles" in str(pitch_failure), (
        f"pitch failure should appear on __context__; got {pitch_failure!r}"
    )

    # Both cleanup attempts were made on the device.
    pitches = [
        args.get("pitch")
        for name, args in env.esp32.tool_calls
        if name == "self.robot.set_head_angles"
    ]
    faces = [
        args.get("face")
        for name, args in env.esp32.tool_calls
        if name == "self.display.set_avatar"
    ]
    assert 24.0 in pitches, "pitch rollback was attempted"
    assert "idle" in faces, "idle avatar restore was attempted"


@pytest.mark.asyncio
async def test_listen_motion_look_up_engine_failure_with_rollback_failure_chains(
    fake_decode,
    fast_sleep,
):
    """Engine failure mid-capture with a failing pitch rollback surfaces
    both: the primary references the engine, the rollback failure is
    chained via ``__cause__`` rather than vanishing into a logger.warning.
    """
    env = _build_env(exc=TimeoutError("model timed out"), frames_to_inject=[b"opus_a"])

    original_call_tool = env.esp32.call_tool

    async def rollback_only_failure(name, arguments):
        # Forward set_head_angles uses pitch=50.0 (look_up_pitch),
        # forward set_avatar uses face='thinking'. Both stay on the
        # original fake path. Only the rollback set_head_angles
        # (saved pitch=24.0) fails here.
        if name == "self.robot.set_head_angles" and arguments.get("pitch") == 24.0:
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "simulated rollback pitch failure"}
        return await original_call_tool(name, arguments)

    env.esp32.call_tool = rollback_only_failure

    with pytest.raises(RuntimeError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 500, "motion": "look-up"},
            gateway=env.gateway,
            registry=env.reg,
        )

    # The engine failure is wrapped into a RuntimeError stating which
    # engine failed; it remains the primary exception so the caller's
    # first signal is still "what triggered the failure".
    primary = exc_info.value
    assert "STT engine" in str(primary) or "engine" in str(primary).lower(), (
        f"primary error should reference the engine failure; got {primary!r}"
    )

    chained = primary.__cause__
    assert chained is not None, (
        "rollback failure must be chained onto the listen failure, "
        "not silently swallowed"
    )
    assert "set_head_angles" in str(chained), (
        f"chained error should reference the rollback head-angles failure; "
        f"got {chained!r}"
    )

    # Sanity: forward motion ran (pitch=50) and rollback was attempted
    # (pitch=24 — that's the call we made fail).
    pitches_attempted = [
        args.get("pitch")
        for name, args in env.esp32.tool_calls
        if name == "self.robot.set_head_angles"
    ]
    assert 50.0 in pitches_attempted
    assert 24.0 in pitches_attempted


@pytest.mark.asyncio
async def test_listen_motion_look_up_nested_partial_failure_surfaces_rollback_error(
    fake_decode,
    fast_sleep,
):
    """Forward-setup failure (set_avatar 'thinking') with a failing
    rollback surfaces both errors: the primary references the forward
    avatar failure, the rollback is chained via ``__cause__``.
    """
    env = _build_env(text="ok", frames_to_inject=[b"opus_a"])

    original_call_tool = env.esp32.call_tool

    async def double_failure(name, arguments):
        # Forward set_avatar('thinking') fails — record attempt.
        if name == "self.display.set_avatar" and arguments.get("face") == "thinking":
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "simulated forward avatar failure"}
        # Rollback set_head_angles(saved pitch=24.0) fails — record attempt.
        if name == "self.robot.set_head_angles" and arguments.get("pitch") == 24.0:
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "simulated rollback pitch failure"}
        # Forward set_head_angles(50.0), forward get_head_angles, and
        # any other tool call use the fake path which records itself.
        return await original_call_tool(name, arguments)

    env.esp32.call_tool = double_failure

    with pytest.raises(RuntimeError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 500, "motion": "look-up"},
            gateway=env.gateway,
            registry=env.reg,
        )

    # The primary exception is the forward avatar failure.
    primary = exc_info.value
    assert "set_avatar" in str(primary) or "avatar" in str(primary), (
        f"primary error should reference the forward avatar failure; got {primary!r}"
    )

    chained = primary.__cause__
    assert chained is not None, (
        "rollback failure must be chained onto the forward failure, "
        "not silently swallowed"
    )
    assert "set_head_angles" in str(chained), (
        f"chained error should reference the rollback head-angles failure; got {chained!r}"
    )

    # Both the forward attempt and the rollback attempt were made.
    avatars_attempted = [
        args.get("face")
        for name, args in env.esp32.tool_calls
        if name == "self.display.set_avatar"
    ]
    assert "thinking" in avatars_attempted, "forward thinking avatar was attempted"
    pitches_attempted = [
        args.get("pitch")
        for name, args in env.esp32.tool_calls
        if name == "self.robot.set_head_angles"
    ]
    assert 50.0 in pitches_attempted, "forward look_up pitch was attempted"
    assert 24.0 in pitches_attempted, "rollback saved pitch was attempted"


@pytest.mark.asyncio
async def test_listen_motion_look_up_partial_rollback_still_restores_avatar(
    fake_decode,
    fast_sleep,
):
    """If the pitch rollback fails during look-up cleanup, the avatar
    restore must still run — a failed listen never leaves the device
    stuck on the ``thinking`` face.
    """
    env = _build_env(exc=TimeoutError("model timed out"), frames_to_inject=[b"opus_a"])

    original_call_tool = env.esp32.call_tool
    avatar_idle_observed = asyncio.Event()

    async def selective_rollback_failure(name, arguments):
        # Fail only on the rollback set_head_angles call (saved
        # pitch=24.0); the forward look-up call uses pitch=50.0 and
        # stays on the original fake path. Record the failing call so
        # the assertion verifies the rollback was actually attempted.
        if name == "self.robot.set_head_angles" and arguments.get("pitch") == 24.0:
            env.esp32.tool_calls.append((name, dict(arguments)))
            return {}, {"message": "simulated rollback failure"}
        result_pair = await original_call_tool(name, arguments)
        if name == "self.display.set_avatar" and arguments.get("face") == "idle":
            avatar_idle_observed.set()
        return result_pair

    env.esp32.call_tool = selective_rollback_failure

    with pytest.raises(RuntimeError):
        await listen_and_transcribe(
            {"duration_ms": 500, "motion": "look-up"},
            gateway=env.gateway,
            registry=env.reg,
        )

    # The rollback set_head_angles was actually attempted (and rejected).
    assert any(
        name == "self.robot.set_head_angles" and args.get("pitch") == 24.0
        for name, args in env.esp32.tool_calls
    ), "pitch rollback should be attempted before the avatar restore"

    # The avatar restore must run regardless of the pitch failure.
    assert avatar_idle_observed.is_set(), (
        "set_avatar('idle') must run even when the pitch rollback raises"
    )


@pytest.mark.asyncio
async def test_pipeline_returns_empty_text_on_no_frames(fake_decode, fast_sleep):
    """An empty capture returns text='' without invoking the engine —
    silence for the full window is not an error.
    """
    env = _build_env(frames_to_inject=[])

    result = await listen_and_transcribe(
        {"duration_ms": 200},
        gateway=env.gateway,
        registry=env.reg,
    )

    assert result["frame_count"] == 0
    assert result["duration_ms"] == 0
    assert result["text"] == ""
    # Engine is NOT invoked when the buffer is empty — wasted work.
    assert env.engine.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("esp32_kwargs", "error_match"),
    [
        ({"protocol_version": 2}, "v1"),
        ({"connected": False}, "ESP32"),
    ],
)
async def test_pipeline_rejects_unsupported_device_state(
    fake_decode, esp32_kwargs, error_match
):
    """Protocol-v2 and disconnected devices fail fast before any work."""
    env = _build_env(**esp32_kwargs)

    with pytest.raises(RuntimeError, match=error_match):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=env.gateway,
            registry=env.reg,
        )

    # No notifications, no engine call, slot stays clean.
    assert env.esp32.listen_states == []
    assert env.engine.calls == []
    assert not is_recording()


@pytest.mark.asyncio
async def test_pipeline_declines_when_device_driven_capture_active():
    """MCP listen() declines when the audio_stream slot is already held
    by a device-driven capture, preserving the active buffer.
    """
    # Simulate a device-driven capture already holding the slot.
    start_recording("device-session-xyz")
    assert is_recording()

    env = _build_env()

    with pytest.raises(RuntimeError, match=r"declined"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=env.gateway,
            registry=env.reg,
        )

    # The pre-existing slot is preserved: no listen.start was sent, no
    # engine call ran, and the device-driven buffer was not clobbered.
    # The autouse cleanup fixture releases it after the test.
    assert env.esp32.listen_states == []
    assert env.engine.calls == []
    assert is_recording()


@pytest.mark.asyncio
async def test_pipeline_translates_disconnect_before_listen_start(
    fake_decode, fast_sleep
):
    """ConnectionError on listen.start surfaces as a clear RuntimeError."""

    class FailingESP32(_FakeESP32):
        async def send_listen_state(self, state: str, mode: str = "manual") -> None:
            self.listen_states.append((state, mode if state == "start" else None))
            if state == "start":
                raise ConnectionError("device dropped during listen.start")

    env = _build_env(esp32_cls=FailingESP32)

    with pytest.raises(RuntimeError, match="listen.start"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=env.gateway,
            registry=env.reg,
        )

    # Recording slot must be closed even when start fails.
    assert not is_recording()
    assert env.engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_translates_engine_error_to_runtime_error(
    fake_decode, fast_sleep
):
    """Engine failure surfaces as RuntimeError with the cause preserved."""
    cause = TimeoutError("model load timed out")
    env = _build_env(exc=cause, frames_to_inject=[b"opus_a"])

    with pytest.raises(RuntimeError) as exc_info:
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=env.gateway,
            registry=env.reg,
        )

    assert "faster-whisper" in str(exc_info.value).lower()
    assert exc_info.value.__cause__ is cause
    # listen.stop was attempted even though transcribe failed (frames
    # arrived, slot needs to drain on the device side).
    assert ("stop", None) in env.esp32.listen_states
    assert not is_recording()


@pytest.mark.asyncio
async def test_pipeline_value_error_propagates_as_value_error(fake_decode, fast_sleep):
    """ValueError from the engine stays a ValueError."""
    env = _build_env(exc=ValueError("bad language hint"), frames_to_inject=[b"opus_a"])

    with pytest.raises(ValueError, match="language"):
        await listen_and_transcribe(
            {"duration_ms": 500},
            gateway=env.gateway,
            registry=env.reg,
        )


@pytest.mark.asyncio
async def test_pipeline_sends_listen_stop_on_cancellation(fake_decode):
    """A cancelled listen() still tells the device to stop — the shielded
    listen.stop send guarantees the firmware leaves listening mode even
    while the orchestrator coroutine itself is being torn down.
    """
    env = _build_env()  # no frame injection; the sleep will be cancelled

    task = asyncio.create_task(
        listen_and_transcribe(
            {"duration_ms": 30000},  # long window; we will cancel mid-flight
            gateway=env.gateway,
            registry=env.reg,
        )
    )
    # Yield twice so the task starts, lands in listen.start, then
    # enters the capture sleep.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Despite the cancellation, both listen.start and listen.stop must
    # have been delivered so the firmware leaves listening mode cleanly.
    state_seq = [s for s, _ in env.esp32.listen_states]
    assert "start" in state_seq
    assert "stop" in state_seq
    # The recording slot must also be released.
    assert not is_recording()
    # Engine is not invoked because the cancellation prevents the
    # post-capture transcribe step.
    assert env.engine.calls == []


@pytest.mark.asyncio
async def test_pipeline_serialises_concurrent_listen_calls(fake_decode, fast_sleep):
    """Concurrent listen() calls are serialised by listen_lock so the
    recording slot stays strictly sequential: start_0 < stop_0 <
    start_1 < stop_1.
    """
    env = _build_env(frames_to_inject=[b"opus_a"])

    await asyncio.gather(
        listen_and_transcribe(
            {"duration_ms": 200}, gateway=env.gateway, registry=env.reg
        ),
        listen_and_transcribe(
            {"duration_ms": 200}, gateway=env.gateway, registry=env.reg
        ),
    )

    state_seq = [s for s, _ in env.esp32.listen_states]
    start_indices = [i for i, s in enumerate(state_seq) if s == "start"]
    stop_indices = [i for i, s in enumerate(state_seq) if s == "stop"]
    assert len(start_indices) == 2
    assert len(stop_indices) == 2
    # The lock guarantees: start_0 < stop_0 < start_1 < stop_1.
    assert start_indices[0] < stop_indices[0] < start_indices[1] < stop_indices[1]
    assert not is_recording()
