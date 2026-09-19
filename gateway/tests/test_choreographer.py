"""Tests for the interaction choreographer (in-conversation body language)."""

import asyncio
import json

from stackchan_mcp.choreographer import (
    Choreographer,
    _talking_steps,
    PRESENCE_MOTIONS,
    ROLE_ENGAGE,
    ROLE_TALK,
    ROLE_THINK,
)


class FakeESP32:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.device_connected = True
        self.angles = {"yaw": 0, "pitch": 40}
        self.fail_angles = False

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "self.robot.get_head_angles":
            if self.fail_angles:
                return None, {"code": -32000, "message": "boom"}
            return {
                "content": [{"type": "text", "text": json.dumps(self.angles)}]
            }, None
        return {"ok": True}, None


class FakeMultiturn:
    turn_count = 1


class FakeGateway:
    def __init__(self):
        self.esp32 = FakeESP32()
        self.multiturn = FakeMultiturn()


def _calls_by(ch, name: str):
    return [a for n, a in ch._gateway.esp32.calls if n == name]


def _role_amps(role: str) -> set[int]:
    """The yaw amplitudes a chooser pick may legitimately carry for a role."""
    return {m.yaw_amp for m in PRESENCE_MOTIONS if m.role == role}


def _role_pitches(role: str) -> set[int]:
    return {m.pitch_amp for m in PRESENCE_MOTIONS if m.role == role}


async def _phase(ch, name: str, *args, **kwargs):
    """Call a fire-and-forget phase method and wait for its body to finish."""
    getattr(ch, name)(*args, **kwargs)
    # The phase spawns its background task synchronously before returning, so
    # if no task exists right after the call, none was started (e.g. a
    # no-op first-tool-flag) and there is nothing to wait on.
    task = ch._active
    if task is not None:
        await task


def run_phase(ch, name: str, *args, **kwargs):
    asyncio.run(_phase(ch, name, *args, **kwargs))


# ---- _talking_steps -------------------------------------------------


def test_talking_steps_size_matches_duration():
    steps = _talking_steps(3000)
    assert steps
    assert sum(s["duration_ms"] for s in steps) == 3000
    assert all(s["shape"] for s in steps)
    assert len(steps) <= 256


def test_talking_steps_zero_and_negative():
    assert _talking_steps(0) == []
    assert _talking_steps(-5) == []


# ---- whole-phase flows ----------------------------------------------


def test_engage_starts_gentle_sway():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "engage")
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "idle"
    assert _calls_by(ch, "self.display.set_blink") == [{"enabled": True}]
    waves = _calls_by(ch, "self.robot.set_head_wave")
    assert len(waves) == 1
    w = waves[0]
    # centered on home, a gentle continuous sway picked from the engage
    # catalog — no discrete set-point hops.
    assert w["center_yaw"] == 0 and w["center_pitch"] == 40
    assert w["yaw_amp"] in _role_amps(ROLE_ENGAGE)
    assert w["yaw_freq_mhz"] > 0
    assert _calls_by(ch, "self.robot.set_head_angles") == []


def test_thinking_starts_mulling_weave():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "thinking")
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "thinking"
    waves = _calls_by(ch, "self.robot.set_head_wave")
    assert len(waves) == 1
    w = waves[0]
    # a mulling weave picked from the thinking catalog — no discrete hops.
    assert w["yaw_amp"] in _role_amps(ROLE_THINK)
    assert w["center_yaw"] == 0 and w["center_pitch"] == 40
    assert w["yaw_freq_mhz"] > 0
    assert _calls_by(ch, "self.robot.set_head_angles") == []


def test_talk_dispatches_mouth_sequence_and_speech_wave():
    ch = Choreographer(FakeGateway())
    run_phase(ch, "talk", 900)
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "happy"
    mouth = _calls_by(ch, "self.display.set_mouth_sequence")
    assert mouth and "steps" in mouth[0]
    assert len(mouth[0]["steps"]) > 0
    waves = _calls_by(ch, "self.robot.set_head_wave")
    assert len(waves) == 1
    w = waves[0]
    # a continuous conversational sway+bob picked from the talk catalog.
    assert w["yaw_amp"] in _role_amps(ROLE_TALK)
    assert w["pitch_amp"] in _role_pitches(ROLE_TALK)
    assert w["center_yaw"] == 0 and w["center_pitch"] == 40
    assert _calls_by(ch, "self.robot.set_head_angles") == []


def test_tool_step_reasserts_weave_for_any_tool():
    for is_first in (True, False):
        ch = Choreographer(FakeGateway())
        run_phase(ch, "tool_step", is_first=is_first)
        waves = _calls_by(ch, "self.robot.set_head_wave")
        assert len(waves) == 1
        assert waves[0]["yaw_amp"] in _role_amps(ROLE_THINK)
        assert _calls_by(ch, "self.robot.set_head_angles") == []


def test_release_restores_home_and_idle():
    ch = Choreographer(FakeGateway())
    ch._home = (10, 40)
    run_phase(ch, "release")
    faces = [a["face"] for a in _calls_by(ch, "self.display.set_avatar")]
    assert faces[-1] == "idle"
    assert _calls_by(ch, "self.display.set_blink") == [{"enabled": True}]
    assert _calls_by(ch, "self.robot.clear_head_wave")  # one stop
    moves = _calls_by(ch, "self.robot.set_head_angles")
    assert moves[-1] == {"yaw": 10, "pitch": 40}


def test_unreadable_home_moves_nothing():
    gw = FakeGateway()
    gw.esp32.fail_angles = True
    ch = Choreographer(gw)
    run_phase(ch, "engage")
    # Face/blink still applied; no head motion without a home pose.
    assert _calls_by(ch, "self.robot.set_head_angles") == []
    assert _calls_by(ch, "self.display.set_avatar")  # face still set


# ---- status catalog & chooser --------------------------------------


def test_catalog_has_variants_per_role():
    for role in (ROLE_ENGAGE, ROLE_THINK, ROLE_TALK):
        variants = [
            m for m in PRESENCE_MOTIONS
            if m.role == role and m.name and m.yaw_amp > 0
        ]
        assert len(variants) > 1, f"{role} needs >1 catalog motion"


def test_list_motions_returns_full_catalog():
    ch = Choreographer(FakeGateway())
    catalog = ch.list_motions()
    # one descriptor per catalog entry, keyed for a client to choose by name.
    assert len(catalog) == len(PRESENCE_MOTIONS)
    assert {e["name"] for e in catalog} == {m.name for m in PRESENCE_MOTIONS}
    assert all(e["role"] and e["description"] for e in catalog)


def test_chooser_avoids_immediate_repeat_within_window():
    ch = Choreographer(FakeGateway())
    # Force many picks; within the repeat window the same name must never
    # appear twice in a row, and consecutive picks alternate across variants.
    names = [ch._pick(ROLE_THINK).name for _ in range(20)]
    for a, b in zip(names, names[1:]):
        assert a != b
    assert len(set(names)) > 1


def test_play_named_motion_starts_that_wave():
    ch = Choreographer(FakeGateway())
    target = next(m for m in PRESENCE_MOTIONS if m.role == ROLE_THINK)
    result = asyncio.run(ch.play(target.name))
    assert result["ok"] is True
    assert result["name"] == target.name and result["role"] == ROLE_THINK
    waves = _calls_by(ch, "self.robot.set_head_wave")
    assert len(waves) == 1 and waves[0]["yaw_amp"] == target.yaw_amp


def test_play_unknown_motion_lists_catalog():
    ch = Choreographer(FakeGateway())
    result = asyncio.run(ch.play("nope-not-a-motion"))
    assert result["ok"] is False
    assert "nope-not-a-motion" in result["error"]
    assert set(result["motions"]) == {m.name for m in PRESENCE_MOTIONS}
