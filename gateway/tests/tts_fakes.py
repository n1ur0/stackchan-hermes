"""Shared fakes for the TTS back-half test suite.

``test_orchestrator``, ``test_send_pcm_audio`` and ``test_send_pcm_stream``
all exercise the same synthesize-and-push back half from different entry
points; the wire-recording device fake is identical for all three, so it
lives here instead of being copy-pasted per file.
"""

from __future__ import annotations

import asyncio


class FakeTTSESP32:
    """Records what reaches the wire so tests can assert event ordering."""

    def __init__(self, *, connected: bool = True) -> None:
        self.device_connected = connected
        self.frames: list[bytes] = []
        self.tts_states: list[str] = []
        # Relative order in which audio frames and TTS state notifications
        # were dispatched, so tests can assert ``start`` precedes any frame
        # and ``stop`` trails them.
        self.events: list[tuple[str, object]] = []
        # Mirror the production manager's per-device TTS lock so the
        # orchestrator's ``async with gateway.esp32.tts_lock`` works the
        # same way under tests as in production. The lock is created
        # per-fake so each test runs against a fresh instance.
        self.tts_lock = asyncio.Lock()

    async def send_audio_frame(self, frame: bytes) -> None:
        self.frames.append(frame)
        self.events.append(("frame", frame))

    async def send_tts_state(self, state: str) -> None:
        self.tts_states.append(state)
        self.events.append(("tts_state", state))


class FakeTTSGateway:
    """Gateway stand-in carrying just the fake device and a status flag."""

    def __init__(self, esp32: FakeTTSESP32) -> None:
        self.esp32 = esp32

    @property
    def device_connected(self) -> bool:
        return bool(self.esp32.device_connected)
