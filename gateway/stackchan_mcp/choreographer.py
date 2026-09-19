"""Coordinated aliveness choreography for the StackChan interaction.

When the robot is just sitting there, :mod:`stackchan_mcp.heartbeat`
already fires occasional idle gestures (glance / expression / nod).
But *during* a voice interaction the heartbeat is deliberately suppressed,
so the robot would otherwise sit statuesque while it listens, thinks,
runs tool steps and speaks. This module is the missing layer: it maps each
phase of a voice turn onto a small, coordinated sequence of body language —
head servo, avatar face, lip-sync mouth and blink — so the device feels
present for the whole conversation, not just at idle. (The phase LED colour
stays owned by the bridge; see below.)

Design rules (match the codebase, see AGENTS.md):

- **Everything is best-effort and non-blocking.** Each phase method does
  *not* block the voice turn: it launches a background task that issues a
  few sequential device calls and swallows every error. A flaky ESP32 call
  must never hold up the turn.
- **One active choreography at a time.** A single per-gateway background
  task owns the head. Starting a new phase cancels the previous one so we
  never interleave competing ``set_head_angles`` commands.
- **Subtle amplitudes.** The head moves are life-cues (±4..8 deg), not
  theatre. They compose with any other movement without looking frantic,
  and they never fight an idling hold.
- **Home-pose safe.** The starting ``(yaw, pitch)`` is read once when the
  conversation begins and the head is returned to it on release.
- **Mouth lip-sync is sized to the spoken reply.** ``set_mouth_sequence``
  plays locally on the device with per-step timers, so the talking pattern
  is built to span (approximately) the TTS ``duration_ms``. It is not
  frame-locked to the audio — by design it only needs to *read* as talking.

The movement amplitudes are tuned for the M5Stack-recommended servo band
(yaw -90..90, pitch 5..85) with a small working envelope, mirroring
:mod:`stackchan_mcp.heartbeat` (YAW_MIN/MAX/PITCH_MIN/MAX helpers).

Phase map (the "workflow" of one turn):

=============  ===========================================================
Phase          Coordinated body language
=============  ===========================================================
engage()       listen opened — face idle, blink on, one small welcome
               glance off home and back (the "noticed you" cue).
thinking()     LLM working — face ``thinking``, a slow lateral "mulling"
               weave (±7 deg yaw, 2 cycles), then rest.
tool_step()    a tool fired — face stays ``thinking``; on the first tool a
               brief consult-tilt (pitch −4 deg, back) as if checking
               something. Later tools keep motion muted (status text is
               the signal).
talk()         reply TTS — face ``happy``, a lip-sync mouth sequence sized
               to the reply, plus 2–3 small speech-pitch nods.
release()      turn over / waiting for follow-up — shut off any active
               motion, restore home pose, face ``idle``, blink on again.
=============  ===========================================================

Env gate: ``STACKCHAN_CHOREOGRAPHER=0`` disables the whole layer.

The phase LED colour is owned by :mod:`stackchan_mcp.control` and the voice
bridge (``apply_led_state``), not by this module; the choreographer only
moves body parts (head, face, mouth, blink). This keeps display honours
single-sourced and lets ``control.restore_idle_led`` settle the LEDs on
every exit path regardless of choreography.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Turn the whole choreography layer off (belt-and-braces alongside the
#: per-phase guards below). Set STACKCHAN_CHOREOGRAPHER=0 to disable.
_ENABLED = os.getenv("STACKCHAN_CHOREOGRAPHER", "1") != "0"

#: Working envelope — life-cue offsets sized to StackChan's WIDE servo range.
#: StackChan's reference servo model (m5stackchan-servo.ts) travels yaw ±90
#: (practical ±128), pitch 0..90, and its touch-stroke wobble swings ±20°.
#: Reachy-scale offsets (±4..7°, valid for a big head) are IMPERCEPTIBLE here
#: (the "stale" complaint) — scale gestures to ~2/3 of the wobble so they read
#: clearly while staying inside the M5Stack-recommended 5..85° pitch band.
YAW_SWING_DEG = 16  # lateral "mulling" weave — clearly rocks the head
YAW_GLANCE_DEG = 14  # welcome glance — an unambiguous turn of the head
PITCH_CONSULT_DEG = 9  # tool consult-tilt
PITCH_NOD_DEG = 9  # speech nod — a visible head bob
YAW_MIN, YAW_MAX = -90, 90
PITCH_MIN, PITCH_MAX = 5, 85

#: Lip-sync pattern: one "mouth syllable" cycle, shapes in talking order.
#: Each shape is a full-face frame (see set_mouth_sequence docs).
TALK_SHAPES = ("half", "open", "u", "half", "e")
TALK_SHAPE_MS = 150  # per-shape hold — ~5 shapes x 150 ms = one "syllable"
MAX_MOUTH_STEPS = 256  # firmware cap for set_mouth_sequence


def _clamp(value: int, lo: int, hi: int) -> int:
    return min(max(value, lo), hi)


def _enabled() -> bool:
    return _ENABLED


def _talking_steps(duration_ms: int) -> list[dict[str, Any]]:
    """Build a lip-sync mouth sequence that spans ~``duration_ms``.

    Returns ≤ ``MAX_MOUTH_STEPS`` steps, each holding a talking mouth shape
    for ``TALK_SHAPE_MS``, cycling the syllable so the face reads as
    speaking for roughly the length of the TTS reply. An empty list means
    "too short / nothing to show" and should not be dispatched.
    """
    if duration_ms <= 0:
        return []
    total_cycles = max(1, duration_ms // (len(TALK_SHAPES) * TALK_SHAPE_MS))
    steps: list[dict[str, Any]] = []
    while len(steps) < MAX_MOUTH_STEPS and len(steps) * TALK_SHAPE_MS < duration_ms:
        for shape in TALK_SHAPES:
            if len(steps) >= MAX_MOUTH_STEPS:
                break
            steps.append({"shape": shape, "duration_ms": TALK_SHAPE_MS})
    if total_cycles and len(steps) == 0:  # pragma: no cover - defensive
        steps = [{"shape": "half", "duration_ms": TALK_SHAPE_MS}]
    return steps


class Choreographer:
    """Per-gateway owner of the in-conversation body language.

    One instance per :class:`~stackchan_mcp.gateway.Gateway` (``gateway.choreo``).
    Holds exactly one running background motion task at a time so head and
    face commands from different phases never interleave.
    """

    def __init__(self, gateway: Any):
        self._gateway = gateway
        #: Home pose captured at engage(); ``None`` until first read.
        self._home: tuple[int, int] | None = None
        self._active: asyncio.Task | None = None

    # ---- device helpers (mirror heartbeat, swallow errors) ----------

    async def _call(self, name: str, args: dict[str, Any]) -> Any:
        if not _enabled():
            return None
        try:
            result, error = await self._gateway.esp32.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - device path is best-effort
            logger.warning("choreographer: %s failed: %s", name, exc)
            return None
        if error:
            logger.warning("choreographer: %s failed: %s", name, error)
            return None
        return result

    async def _read_home(self) -> tuple[int, int] | None:
        """Current (yaw, pitch), parsed like heartbeat does, or None."""
        result = await self._call("self.robot.get_head_angles", {})
        if not isinstance(result, dict):
            return None
        content = result.get("content")
        if isinstance(content, list) and content:
            item = content[0]
            text = item.get("text", "") if isinstance(item, dict) else ""
            import json

            try:
                result = json.loads(text)
            except (ValueError, TypeError):
                return None
        try:
            return int(result["yaw"]), int(result["pitch"])
        except (KeyError, TypeError, ValueError):
            return None

    async def _set_face(self, face: str) -> None:
        await self._call("self.display.set_avatar", {"face": face})

    async def _set_blink(self, enabled: bool) -> None:
        await self._call("self.display.set_blink", {"enabled": enabled})

    async def _move_head(self, yaw: int, pitch: int) -> None:
        yaw = _clamp(yaw, YAW_MIN, YAW_MAX)
        pitch = _clamp(pitch, PITCH_MIN, PITCH_MAX)
        await self._call("self.robot.set_head_angles", {"yaw": yaw, "pitch": pitch})

    # ---- background task management --------------------------------

    def _cancel_active(self) -> None:
        if self._active is not None and not self._active.done():
            self._active.cancel()
        self._active = None

    def _spawn(self, coro) -> None:
        self._cancel_active()
        self._active = asyncio.create_task(coro)

    # ---- phase methods (each is fire-and-forget) -------------------

    def engage(self) -> None:
        """Conversation just opened (listen started / wake word / tap)."""
        if not _enabled():
            return

        async def _body() -> None:
            if self._gateway.esp32 is None or not getattr(
                self._gateway.esp32, "device_connected", False
            ):
                return
            if self._home is None:
                self._home = await self._read_home()
            await self._set_face("idle")
            await self._set_blink(True)
            home = self._home
            if home is not None:
                yaw, pitch = home
                side = (
                    1
                    if (getattr(self._gateway.multiturn, "turn_count", 1) % 2 == 0)
                    else -1
                )
                await self._move_head(
                    _clamp(yaw + YAW_GLANCE_DEG * side, YAW_MIN, YAW_MAX), pitch
                )
                await asyncio.sleep(1.0)
                await self._move_head(yaw, pitch)
            else:
                await asyncio.sleep(0.6)

        self._spawn(_body())

    def thinking(self) -> None:
        """LLM is working — pensive lateral weave behind ``Thinking...``."""
        if not _enabled():
            return

        async def _body() -> None:
            await self._set_face("thinking")
            home = self._home or await self._read_home()
            if home is None:
                await asyncio.sleep(0.5)
                return
            yaw, pitch = home
            try:
                for _ in range(2):
                    for side in (1, -1):
                        await self._move_head(
                            _clamp(yaw + YAW_SWING_DEG * side, YAW_MIN, YAW_MAX),
                            pitch,
                        )
                        await asyncio.sleep(1.1)
            except asyncio.CancelledError:
                logger.debug("choreographer: thinking weave cancelled")
                raise

        self._spawn(_body())

    def tool_step(self, *, is_first: bool) -> None:
        """A Hermes tool fired — subtle consult-tilt on the first one only."""
        if not _enabled() or not is_first:
            return

        async def _body() -> None:
            home = self._home or await self._read_home()
            if home is None:
                return
            yaw, pitch = home
            await self._move_head(
                yaw, _clamp(pitch - PITCH_CONSULT_DEG, PITCH_MIN, PITCH_MAX)
            )
            await asyncio.sleep(0.5)
            await self._move_head(yaw, pitch)

        self._spawn(_body())

    def talk(self, duration_ms: int) -> None:
        """Reply is about to play — happy face + sized lip-sync + small nods.

        ``duration_ms`` comes from the TTS result so the mouth roughly spans
        the spoken reply (see module docstring re: lip-sync being
        approximate by design).
        """
        if not _enabled():
            return

        async def _body() -> None:
            await self._set_face("happy")
            steps = _talking_steps(duration_ms)
            if steps:
                await self._call("self.display.set_mouth_sequence", {"steps": steps})
            home = self._home or await self._read_home()
            budget_s = max(0.0, (duration_ms / 1000.0) - 0.4)  # leave a tail
            if home is not None and budget_s > 0:
                yaw, pitch = home
                try:
                    # interval = one nod up+down; do 2-3 gentle ones.
                    period = min(1.8, max(0.9, budget_s / 3.0))
                    start = asyncio.get_running_loop().time()
                    while (asyncio.get_running_loop().time() - start) < budget_s:
                        await self._move_head(
                            yaw, _clamp(pitch - PITCH_NOD_DEG, PITCH_MIN, PITCH_MAX)
                        )
                        await asyncio.sleep(period / 2.0)
                        await self._move_head(yaw, pitch)
                        await asyncio.sleep(period / 2.0)
                except asyncio.CancelledError:
                    logger.debug("choreographer: speech nods cancelled")
                    await self._move_head(yaw, pitch)
                    raise

        self._spawn(_body())

    def release(self, *, happy: bool = False) -> None:
        """End of turn / waiting for a follow-up — settle to home, idle face."""
        if not _enabled():
            return

        async def _body() -> None:
            await self._set_face("happy" if happy else "idle")
            await self._set_blink(True)
            home = self._home
            if home is not None:
                yaw, pitch = home
                await self._move_head(yaw, pitch)
            self._home = None

        # release short-circuits any in-flight phase directly (its own task).
        self._cancel_active()
        self._active = asyncio.create_task(_body())
